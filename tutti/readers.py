"""Automatic usage readings, with no input from the owner.

Three sources; the first two read what the CLIs print:
- an account's `reader`: a command the CLI answers without spending quota (Antigravity: `agy -p /usage --output-format json`
  prints every quota bucket with its remaining fraction and reset time); `plan` reads a label such as the subscription tier
  (`cursor-agent about --format json`);
- limit messages in the output of jobs Tutti ran (`tutti quiet` logs, `tutti run`): a "usage limit reached, resets in 3 h"
  line means the account is spent until then;
- Cursor's own account call (`reader: {format: cursor-api, field: ...}`, tutti/cursorusage.py), signed in with the token of
  cursor-agent's login: the spending page's two meters with no owner click. The token is never logged or printed.
Parsing is pure; running commands and appending to the usage log are the thin edge at the bottom."""
import glob
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

from . import config, cursorusage, pacing

READER_TIMEOUT = 90
SCAN_DAYS = 2
CONTEXT_LINES = 2
# Keep a new automatic reading only when it says something new: a change of a point or more, a reset that moved, or an hour
# since the last one (so the sparkline keeps a point per hour without a line every few minutes).
MIN_CHANGE = 1.0
RESET_MOVED = timedelta(minutes=10)
KEEP_EVERY = timedelta(hours=1)
# A reader whose answer did not change is asked half as often next time, down to once every BACKOFF_MAX intervals; any
# change, or a Refresh readings click, brings it back to its own interval. agy's /usage answer is byte for byte the same
# while nothing ran, so an idle dashboard spawned it 120 times an hour for nothing (owner, 2026-11-14).
BACKOFF_MAX = 8

LIMITS = tuple(re.compile(p, re.I) for p in (
    r'usage limit', r'rate[ -]?limit', r'quota (?:exceeded|exhausted|reached|limit)', r'resource[_ ]exhausted',
    r'limit (?:reached|exceeded|hit)', r'out of (?:credits|requests|quota)', r'(?:hit|reached) your .{0,40}limit',
    r'reached the .{0,40}limit', r'too many requests', r'exhausted your .{0,30}quota'))
ISO = re.compile(r'\d{4}-\d\d-\d\d[T ]\d\d:\d\d[\d:.]*(?:Z|[+-][\d:]{4,5})?')   # parse_time checks the rest
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


def github_premium(data, allowance, now):
    """(used %, reset) from GitHub's premium-request usage report (GET /users/<login>/settings/billing/premium_request/usage):
    the Copilot premium requests of this month against the plan's `allowance`; the month resets on the 1st, 00:00 UTC."""
    items = (data or {}).get('usageItems') if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError('no usageItems in the answer: is this the premium_request/usage report?')
    used = sum(float(i.get('grossQuantity') or 0) for i in items if 'copilot' in str(i.get('product', '')).lower())
    first = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return round(100.0 * used / float(allowance), 1), pacing.shift(first, 'monthly', 1)


CREDITS = re.compile(r'\bAI Credits\s+([\d.]+)')


def credits_in(lines):
    """The AI credits a `copilot -p` run printed at its end ("AI Credits 6.84 (3s)"), summed over the lines given."""
    total = 0.0
    for line in lines:
        m = CREDITS.search(line)
        if m:
            try:
                total += float(m.group(1))
            except ValueError:
                continue
    return round(total, 2)


def busy_intervals(folder, worker, now):
    """[(start, end)] of Tutti's runs of one worker, from `<state>/runs/<epoch>-<pid>.log.cmd` ("run <worker>: ..."): the
    start from the name, the end from the `.rc` file's time (still running: now). The planner's background estimate skips them."""
    out = []
    for cmd in glob.glob(os.path.join(folder, '*.log.cmd')):
        try:
            with open(cmd, encoding='utf8', errors='replace') as f:
                first = f.readline()
            start = float(os.path.basename(cmd).split('-')[0])
        except (OSError, ValueError):
            continue
        if not first.startswith(f'run {worker}:'):
            continue
        end = mtime(cmd[:-len('.cmd')] + '.rc')
        out.append((datetime.fromtimestamp(start, timezone.utc),
                    datetime.fromtimestamp(end, timezone.utc) if end else now))
    return out


def credits_since(folders, since):
    """AI credits printed by agent runs whose logs changed since `since` (a datetime), in the given folders (Tutti's runs
    and quiet logs)."""
    total = 0.0
    for folder in folders:
        for path in glob.glob(os.path.join(folder, '*.log')):
            seen = mtime(path)
            if seen is None or seen < since.timestamp():
                continue
            try:
                with open(path, encoding='utf8', errors='replace') as f:
                    total += credits_in(f)
            except OSError:
                continue
    return round(total, 2)


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


HINTS = (re.compile(r'needs the "?\w+"? scope', re.I), re.compile(r'(?:gh )?auth (?:login|refresh)[^\n]*'),
         re.compile(r'(?:sign|log) in[^\n]*', re.I))


def sign_in_hint(stderr):
    """What the CLI said to do about signing in or a missing scope (gh: `gh auth refresh -h github.com -s user`), or ''.
    Only such lines are kept: never anything that looks like a token."""
    found = [m.group(0).strip() for rx in HINTS for m in rx.finditer(stderr or '') if 'token' not in m.group(0).lower()]
    return '; '.join(dict.fromkeys(found))[:300]


def run_json(command, timeout=READER_TIMEOUT, cwd=None):
    """Runs a reader command and parses its JSON answer. Returns (data, problem); only stdout is read."""
    try:
        r = subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd=cwd or os.path.expanduser('~'))
    except FileNotFoundError:
        return None, f'{command[0]} is not on PATH: install it and sign in once'
    except subprocess.TimeoutExpired:
        return None, f'{" ".join(command)} did not answer within {timeout} s; it is asked again at the next reading'
    if r.returncode != 0:
        hint = sign_in_hint(r.stderr)
        return None, f'{" ".join(command)} exited {r.returncode}: ' + (hint or 'is it signed in? Run it once by hand to see why')
    try:
        return json.loads(r.stdout), None
    except ValueError:
        return None, f'{" ".join(command)} did not print JSON: the reader expects --output-format json (or --format json)'


def key_of(spec):
    """The cache key of a reader: its command (one run serves every account that shares it), or its format."""
    return tuple(spec.get('command') or [spec.get('format') or '?'])


class Readers:
    """Runs each reader command at most every `every_minutes` (default: every probe, `dashboard.probe_seconds`); one run
    serves every account that shares the command. `force` (Refresh readings, `tutti usage refresh`) skips the wait."""

    def __init__(self, cfg, run=run_json, clock=time.time, cursor=None):
        self.cfg, self.run, self.clock = cfg, run, clock
        self.cursor = cursor or cursorusage.fetch   # format cursor-api: Cursor's account call, no command
        self.cache = {}   # command tuple -> (when, data, problem)
        self.backoff = {}   # command tuple -> how many intervals to wait now (1 .. BACKOFF_MAX)
        self.runs = 0   # reader commands run, for `tutti dashboard` measurements
        self.scanned = {}   # job log -> mtime already scanned

    def every_minutes(self, spec):
        return spec.get('every_minutes') or round(float(self.cfg['dashboard']['probe_seconds']) / 60, 2)

    def _answer(self, spec, force=False):
        key = key_of(spec)
        every = float(self.every_minutes(spec)) * 60
        hit = self.cache.get(key)
        wait = every * self.backoff.get(key, 1)
        # A little under the interval: a probe that comes a moment early still runs the reader.
        if hit and not force and self.clock() - hit[0] < wait * 0.9:
            return hit[1], hit[2]
        data, problem = self.cursor() if spec.get('format') == 'cursor-api' else self.run(list(spec['command']))
        self.runs += 1
        same = hit is not None and not force and problem is None and data is not None and data == hit[1]
        self.backoff[key] = min(BACKOFF_MAX, self.backoff.get(key, 1) * 2) if same else 1
        self.cache[key] = (self.clock(), data, problem)
        return data, problem

    def interval_minutes(self, spec):
        """The interval a reader is asked at now: its own, times the back-off while its answer stays the same."""
        return round(float(self.every_minutes(spec)) * self.backoff.get(key_of(spec), 1), 2)

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
            data, problem = self._answer(r, force)
            out['every_minutes'] = self.interval_minutes(r)
            if data is not None:
                try:
                    out['used'], out['resets'] = self.parse(r, a, data, now or datetime.fromtimestamp(self.clock(), timezone.utc))
                except (ValueError, KeyError, TypeError) as e:
                    problem = f'accounts.{name}.reader: {e}'
                    self.backoff[key_of(r)] = 1   # an answer it cannot read is asked again at its own interval
            out['problem'] = problem or out['problem']
        return out

    @staticmethod
    def parse(r, a, data, now):
        """(used %, reset) from a reader's answer: GitHub's premium-request report, else agy's /usage buckets."""
        if r.get('format') == 'github-premium':
            used, resets = github_premium(data, r['allowance'], now)
        elif r.get('format') == 'cursor-api':
            used, resets = cursorusage.parse(data, r['field'])
            if resets is None:
                return used, None
        else:
            used, resets = agy_usage(data, r['bucket'])
        return used, not_started(resets, a.get('window'), now)

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


def parse_reading_file(text, account=None):
    """(used, resets or None, at or None) from a reading file: `42`, `42 %`, {"used": 42, "resets": ISO, "at": ISO}, or one
    file for several accounts: {"claude": {"used": 7, "resets": ...}, "claude-fable": {"used": 3}} (or {"claude": 7})."""
    text = text.strip()
    if text.startswith('{'):
        data = json.loads(text)
        if account is not None and account in data:
            data = data[account] if isinstance(data[account], dict) else {'used': data[account]}
        elif 'used' not in data:
            raise ValueError(f'no "{account}" in the file: write {{"{account}": {{"used": 42}}}} or {{"used": 42}}')
        used, resets, at = data.get('used'), data.get('resets'), data.get('at')
    else:
        used, resets, at = text.rstrip('%').strip(), None, None
    used = float(used)
    if not 0 <= used <= 100:
        raise ValueError(f'used is {used:g}: write a percentage from 0 to 100')
    return used, pacing.parse_time(resets) if resets else None, pacing.parse_time(at) if at else None


def record_files(path, accounts):
    """Readings an orchestrator wrote to accounts.<name>.reading_file (it gets the plan's % from its own tool, which no CLI
    prints). Each version of the file is logged once (its ref holds the file's mtime). Returns (lines written, problems)."""
    entries = pacing.read_log(path)
    refs = {e.get('ref') for e in entries if e.get('ref')}
    written, problems = [], []
    for name, a in accounts.items():
        f = config.expand((a or {}).get('reading_file') or '') if (a or {}).get('reading_file') else None
        seen = mtime(f) if f else None
        ref = f'file:{f}:{name}:{seen}'   # one file may hold several accounts: each is logged once per version
        if seen is None or ref in refs:
            continue
        try:
            with open(f, encoding='utf8') as fh:
                used, resets, at = parse_reading_file(fh.read(), name)
        except (OSError, ValueError, TypeError) as e:
            problems.append(f'accounts.{name}.reading_file {f}: {e}: write a percentage such as 42, or {{"used": 42}}')
            continue
        written.append(pacing.append_log(path, name, used, at or datetime.fromtimestamp(seen, timezone.utc), 'from the reading file',
                                         resets=resets, source='file', ref=ref))
    return written, problems


def utcnow():
    return datetime.now(timezone.utc)
