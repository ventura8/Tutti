"""Automatic usage readings, with no input from the owner.

Two sources, both read from what the CLIs print, never from their credential files or private APIs:
- an account's `reader`: a command the CLI answers without spending quota (Antigravity: `agy -p /usage --output-format json`
  prints every quota bucket with its remaining fraction and reset time); `plan` reads a label such as the subscription tier
  (`cursor-agent about --format json`);
- limit messages in the output of jobs Tutti ran (`tutti quiet` logs, `tutti run`): a "usage limit reached, resets in 3 h"
  line means the account is spent until then.
Parsing is pure; running commands and appending to the usage log are the thin edge at the bottom."""
import glob
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

from . import config, pacing

READER_TIMEOUT = 90
SCAN_DAYS = 2
CONTEXT_LINES = 2
# Keep a new automatic reading only when it says something new: a change of a point or more, a reset that moved, or an hour
# since the last one (so the sparkline keeps a point per hour without a line every few minutes).
MIN_CHANGE = 1.0
RESET_MOVED = timedelta(minutes=10)
KEEP_EVERY = timedelta(hours=1)

LIMITS = tuple(re.compile(p, re.I) for p in (
    r'usage limit', r'rate[ -]?limit', r'quota (?:exceeded|exhausted|reached|limit)', r'resource[_ ]exhausted',
    r'limit (?:reached|exceeded|hit)', r'out of (?:credits|requests|quota)', r'(?:hit|reached) your .{0,40}limit',
    r'reached the .{0,40}limit', r'too many requests', r'exhausted your .{0,30}quota'))
ISO = re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?')
UNIT = r'(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b'
RELATIVE = re.compile(r'\b(?:in|after|within)\s+((?:\d+(?:\.\d+)?\s*' + UNIT + r'[\s,]*(?:and\s+)?)+)', re.I)
PART = re.compile(r'(\d+(?:\.\d+)?)\s*' + UNIT, re.I)
SECONDS = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
CLOCK = re.compile(r'\b(?:resets?|refresh(?:es)?|available|again)\b\D{0,12}?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b', re.I)


def _unit_seconds(unit):
    return SECONDS[unit.lower()[0]]


def parse_reset(text, now):
    """The reset time a limit message names: an ISO time, or `in 3h 12m` / `in 45 minutes` / `in 6 days, 12 hours`; else None."""
    m = ISO.search(text)
    if m:
        try:
            return pacing.parse_time(m.group(0).replace(' ', 'T', 1))
        except ValueError:
            pass
    m = RELATIVE.search(text)
    if not m:
        return clock_reset(text, now)
    total = sum(float(n) * _unit_seconds(u) for n, u in PART.findall(m.group(1)))
    return now + timedelta(seconds=total) if total else None


def clock_reset(text, now):
    """`resets 3pm` / `resets at 15:30`: the next such time of day, in this computer's time zone."""
    m = CLOCK.search(text)
    if not m:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or '').lower()
    if not (m.group(2) or half):
        return None   # a bare number ("again 3") is not a time
    if half:
        hour = hour % 12 + (12 if half == 'pm' else 0)
    if hour > 23 or minute > 59:
        return None
    local = now.astimezone()
    at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (at if at > local else at + timedelta(days=1)).astimezone(timezone.utc)


def limit_hits(lines, now):
    """[(line number, reset or None, the text around it)] for every line that says a limit was reached."""
    hits = []
    for i, line in enumerate(lines):
        if not any(p.search(line) for p in LIMITS):
            continue
        around = ' '.join(lines[i:i + 1 + CONTEXT_LINES])
        hits.append((i, parse_reset(around, now), around[:400]))
    return hits


def match_account(accounts, cmd_text, around):
    """The account a limit message in a job belongs to: the accounts whose `match.command` fits the job's command line; when
    several do, the one whose `match.model` names the model in the command or the message, else the one marked `fallback`."""
    fits = [(n, a['match']) for n, a in accounts.items() if (a or {}).get('match') and re.search(a['match']['command'], cmd_text)]
    if len(fits) <= 1:
        return fits[0][0] if fits else None
    both = f'{cmd_text}\n{around}'
    named = [n for n, m in fits if m.get('model') and re.search(m['model'], both, re.I)]
    if len(named) == 1:
        return named[0]
    fallback = [n for n, m in fits if m.get('fallback')]
    return fallback[0] if fallback else None


def agy_usage(data, bucket):
    """(used %, reset) of one bucket of `agy -p /usage --output-format json`, or a ValueError naming what is missing."""
    groups = (((data or {}).get('command') or {}).get('data') or {}).get('groups')
    if not isinstance(groups, list):
        raise ValueError('no command.data.groups in the answer: is this `agy -p /usage --output-format json`?')
    seen = []
    for g in groups:
        for b in (g or {}).get('buckets') or []:
            seen.append(str(b.get('id')))
            if b.get('id') == bucket:
                used = round(100.0 * (1.0 - float(b['remaining_fraction'])), 1)
                return used, pacing.parse_time(b['reset_time'])
    raise ValueError(f'no bucket "{bucket}" in the answer; the buckets are {", ".join(seen) or "(none)"}')


def not_started(resets, window, now):
    """A rolling window nobody has started yet is reported as resetting one full window after the CLI's own clock, a few
    seconds past ours; it would then start just after `now` and the reading would fall in the window before. Clamp it."""
    if window and pacing.shift(resets, window, -1) > now:
        return pacing.shift(now, window, 1)
    return resets


def json_field(data, field):
    v = (data or {}).get(field) if isinstance(data, dict) else None
    if v is None:
        raise ValueError(f'no "{field}" in the answer')
    return str(v)


# ---- the edge: running reader commands, scanning job logs, appending readings


def run_json(command, timeout=READER_TIMEOUT, cwd=None):
    """Runs a reader command and parses its JSON answer. Returns (data, problem); only stdout is read."""
    try:
        r = subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd=cwd or os.path.expanduser('~'))
    except FileNotFoundError:
        return None, f'{command[0]} is not on PATH: install it and sign in once'
    except subprocess.TimeoutExpired:
        return None, f'{" ".join(command)} did not answer within {timeout} s; it is asked again at the next reading'
    if r.returncode != 0:
        return None, f'{" ".join(command)} exited {r.returncode}: is it signed in? Run it once by hand to see why'
    try:
        return json.loads(r.stdout), None
    except ValueError:
        return None, f'{" ".join(command)} did not print JSON: the reader expects --output-format json (or --format json)'


class Readers:
    """Runs each reader command at most every `every_minutes` (default: every probe, `dashboard.probe_seconds`); one run
    serves every account that shares the command. `force` (Refresh readings, `tutti usage refresh`) skips the wait."""

    def __init__(self, cfg, run=run_json, clock=time.time):
        self.cfg, self.run, self.clock = cfg, run, clock
        self.cache = {}   # command tuple -> (when, data, problem)
        self.scanned = {}   # job log -> mtime already scanned

    def every_minutes(self, spec):
        return spec.get('every_minutes') or round(float(self.cfg['dashboard']['probe_seconds']) / 60, 2)

    def _answer(self, spec, force=False):
        key = tuple(spec['command'])
        every = float(self.every_minutes(spec)) * 60
        hit = self.cache.get(key)
        # A little under the interval: a probe that comes a moment early still runs the reader.
        if hit and not force and self.clock() - hit[0] < every * 0.9:
            return hit[1], hit[2]
        data, problem = self.run(list(spec['command']))
        self.cache[key] = (self.clock(), data, problem)
        return data, problem

    def read(self, name, a, now=None, force=False):
        """{'plan', 'used', 'resets', 'problem', 'every_minutes'} for one account from its `reader` and `plan` commands."""
        out = {'plan': None, 'used': None, 'resets': None, 'problem': None, 'every_minutes': None}
        if a.get('plan'):
            data, problem = self._answer(a['plan'], force)
            try:
                out['plan'] = json_field(data, a['plan']['field']) if data is not None else None
            except ValueError as e:
                problem = f'accounts.{name}.plan: {e}'
            out['problem'] = problem
        r = a.get('reader')
        if r:
            out['every_minutes'] = self.every_minutes(r)
            data, problem = self._answer(r, force)
            if data is not None:
                try:
                    out['used'], out['resets'] = agy_usage(data, r['bucket'])
                    out['resets'] = not_started(out['resets'], a.get('window'), now or datetime.fromtimestamp(self.clock(), timezone.utc))
                except (ValueError, KeyError, TypeError) as e:
                    problem = f'accounts.{name}.reader: {e}'
            out['problem'] = problem or out['problem']
        return out

    def read_all(self, now=None, force=False):
        """Every account with a reader or plan. Pass the `now` the readings will be recorded at."""
        accounts = {n: a or {} for n, a in self.cfg['accounts'].items()}
        return {n: self.read(n, a, now, force) for n, a in accounts.items() if a.get('reader') or a.get('plan')}

    def scan_jobs(self, now, folder=None):
        """[(account, reset, ref, text)] for limit messages in recent `tutti quiet` logs of CLI-agent jobs not scanned yet."""
        folder = folder or os.path.join(config.expand(self.cfg['project']['state']), 'quiet')
        out = []
        for path in glob.glob(os.path.join(folder, '*.log')):
            seen = mtime(path)
            if seen is None or now.timestamp() - seen > SCAN_DAYS * 86400 or self.scanned.get(path) == seen:
                continue
            self.scanned[path] = seen
            out += scan_log(self.cfg['accounts'], path, now)
        return out


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def scan_log(accounts, path, now):
    try:
        with open(path + '.cmd', encoding='utf8', errors='replace') as f:
            cmd_text = f.read()
        with open(path, encoding='utf8', errors='replace') as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for i, reset, around in limit_hits(lines, now):
        name = match_account(accounts, cmd_text, around)
        if name:
            out.append((name, reset, f'{os.path.basename(path)}:{i + 1}', around))
    return out


def scan_text(accounts, cmd_text, lines, now, ref):
    """Limit messages in a worker's output (`tutti run`), attributed like a job log's."""
    out = []
    for i, reset, around in limit_hits(lines, now):
        name = match_account(accounts, cmd_text, around)
        if name:
            out.append((name, reset, f'{ref}:{i + 1}', around))
    return out


def _last_auto(entries, account):
    best = None
    for e in entries:
        if e.get('account') == account and e.get('source') == 'reader' and (best is None or e['at'] > best['at']):
            best = e
    return best


def worth_keeping(last, used, resets, now):
    if not last:
        return True
    if abs(float(last['used']) - used) >= MIN_CHANGE or now - pacing.parse_time(last['at']) >= KEEP_EVERY:
        return True
    old = last.get('resets')
    return used > 0 and (not old or abs(pacing.parse_time(old) - resets) >= RESET_MOVED)


def record(path, readings, hits, now):
    """Appends the readers' readings that say something new and the limit messages not logged yet. Returns the lines written."""
    entries = pacing.read_log(path)
    refs = {e.get('ref') for e in entries if e.get('ref')}
    written = []
    for name, r in readings.items():
        if r.get('used') is None or not worth_keeping(_last_auto(entries, name), r['used'], r['resets'], now):
            continue
        written.append(pacing.append_log(path, name, r['used'], now, 'read automatically', resets=r['resets'], source='reader'))
    for name, reset, ref, _text in hits:
        if ref in refs:
            continue
        refs.add(ref)
        note = f'a limit message in {ref}' + ('' if reset else ' (no reset time in it: the window rule applies)')
        written.append(pacing.append_log(path, name, 100, now, note, resets=reset, source='limit message', ref=ref))
    return written


def utcnow():
    return datetime.now(timezone.utc)
