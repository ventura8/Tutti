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
import math
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

from . import browserusage, codexusage, config, cursorusage, pacing

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
UNIT = r'(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])'   # "147h44m29s": a unit may touch the next number
RELATIVE = re.compile(r'\b(?:in|after|within)\s+((?:\d+(?:\.\d+)?\s*' + UNIT + r'[\s,]*(?:and\s+)?)+)', re.I)
PART = re.compile(r'(\d+(?:\.\d+)?)\s*' + UNIT, re.I)
SECONDS = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
ZONE = re.compile(r'\(?\b(UTC|GMT|[A-Z][A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?)\b\)?')   # "resets 3pm (Europe/Bucharest)"
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
    """`resets 3pm` / `resets at 15:30`: the next such time of day, in the zone the text names ("(Europe/Bucharest)", "UTC"),
    else in this computer's (time.zone), daylight saving included."""
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
    from tutti import tz
    named = ZONE.search(text[m.end():m.end() + 40])
    z = tz.zone('UTC' if named and named.group(1) == 'GMT' else named.group(1)) if named else tz.zone()
    local = tz.local(now, z)
    at = local.replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None)
    if at <= local.replace(tzinfo=None):
        at += timedelta(days=1)
    return tz.to_utc(at, z)


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
    named = [(n, re.search(m['model'], both, re.I)) for n, m in fits if m.get('model')]
    named = sorted(((n, len(x.group(0))) for n, x in named if x), key=lambda t: -t[1])
    if named and (len(named) == 1 or named[0][1] > named[1][1]):
        return named[0][0]   # the most specific model wins: `gemini-4-argon` beats `gemini` for a per-model cap
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
                return bucket_usage(g, b)
    raise ValueError(f'no bucket "{bucket}" in the answer; the buckets are {", ".join(seen) or "(none)"}')


def bucket_usage(group, b):
    """(used %, reset) of one agy bucket. A `disabled` bucket (agy turns the 5h window off while the group's weekly is spent)
    counts as 100 % used until the group's weekly bucket resets; any other bucket without `reset_time` reads with reset None."""
    used = 100.0 if b.get('disabled') else round(100.0 * (1.0 - float(b['remaining_fraction'])), 1)
    reset = b.get('reset_time')
    if b.get('disabled'):
        weekly = [w['reset_time'] for w in (group or {}).get('buckets') or []
                  if w is not b and 'weekly' in str(w.get('id')) and w.get('reset_time')]
        reset = max(weekly, key=pacing.parse_time) if weekly else reset
    return used, pacing.parse_time(reset) if reset else None


def github_premium(data, allowance, now):
    """(used %, reset) from GitHub's premium-request usage report (GET /users/<login>/settings/billing/premium_request/usage):
    the Copilot premium requests of this month against the plan's `allowance`; the month resets at midnight UTC on the 1st."""
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


def observation_keep_seconds(account):
    """Retain real samples often enough for the account's configured stale threshold."""
    hours = account.get('stale_hours') or pacing.STALE_HOURS
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or hours <= 0:
        hours = pacing.STALE_HOURS
    return min(KEEP_EVERY.total_seconds(), max(1.0, hours * 1800))


def key_of(spec):
    """The cache key of a reader: its command (one run serves every account that shares it), or its format."""
    return tuple(spec.get('command') or [spec.get('format') or '?', spec.get('url') or '', spec.get('account') or ''])


def automatic_reader(name, account):
    """A provider-specific reader for an existing account that has not set one explicitly."""
    if account.get('reader'):
        return account['reader']
    if account.get('worker') == 'codex' or name == 'codex':
        return {'format': 'codex-app-server', 'every_minutes': 15}
    if account.get('usage_url'):
        return {'format': 'browser-usage', 'url': account['usage_url'], 'every_minutes': 15}
    return None


def remote_codex_problem(cfg, name, account):
    """Refuse a local reading when Codex is signed in on a different machine."""
    if account.get('_read_here'):
        return None
    worker = (cfg.get('workers') or {}).get(account.get('worker') or 'codex') or {}
    machine = worker.get('machine')
    host = (cfg.get('machines') or {}).get(machine) or {}
    if (automatic_reader(name, account) or {}).get('format') == 'codex-app-server' and machine and host.get('transport') != 'local':
        return f'accounts.{name}.read_on is missing: set it to {machine} to read Codex usage on its login host'
    return None


def account_reader_spec(reader, name, account):
    if reader.get('format') == 'codex-app-server':
        return {**reader, 'account': name}
    if reader.get('format') == 'browser-usage':
        return {**reader, 'url': account.get('usage_url')}
    return reader


def bridge_as_of(name, reading, now):
    """A cached observation must not become current if this host's clock moves backwards."""
    out = dict(reading)
    if out.get('used') is not None and pacing.parse_time(out['observed_at']) > now:
        out.update(used=None, problem=f'accounts.{name}: bridge observation is in the future; check host clocks and retry')
    return out


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

    def _answer(self, spec, force=False, observed_at=None, max_age=None):
        key = key_of(spec)
        every = float(self.every_minutes(spec)) * 60
        hit = self.cache.get(key)
        wait = every * self.backoff.get(key, 1)
        if max_age is not None:
            wait = min(wait, max_age)
        # A little under the interval: a probe that comes a moment early still runs the reader.
        if hit and not force and self.clock() - hit[0] < wait * 0.9:
            return hit[1], hit[2]
        observed_at = observed_at or datetime.fromtimestamp(self.clock(), timezone.utc)
        if spec.get('format') == 'cursor-api':
            data, problem = self.cursor()
        elif spec.get('format') == 'codex-app-server':
            try:
                data, problem = codexusage.read(self.cfg, self.cfg['accounts'].get(spec.get('account'), {})), None
            except (OSError, ValueError, KeyError, TypeError) as e:
                data, problem = None, str(e)
        elif spec.get('format') == 'browser-usage':
            try:
                data, problem = browserusage.read(self.cfg, spec['url']), None
            except (OSError, ValueError, KeyError, TypeError) as e:
                data, problem = None, str(e)
        else:
            data, problem = self.run(list(spec['command']))
        self.runs += 1
        same = hit is not None and not force and problem is None and data is not None and data == hit[1]
        self.backoff[key] = min(BACKOFF_MAX, self.backoff.get(key, 1) * 2) if same else 1
        self.cache[key] = (self.clock(), data, problem, observed_at.isoformat())
        return data, problem

    def interval_minutes(self, spec):
        """The interval a reader is asked at now: its own, times the back-off while its answer stays the same."""
        return round(float(self.every_minutes(spec)) * self.backoff.get(key_of(spec), 1), 2)

    def read_there(self, name, a, force=False, now=None):
        """An account read by the computer that holds its sign-in (accounts.<n>.read_on, set by `tutti move`), through that
        computer's bridge; asked at the reader's own interval like a reader run here."""
        from . import bridge
        spec = automatic_reader(name, a) or a.get('plan') or {}
        key = ('bridge', name)
        hit = self.cache.get(key)
        wait = min(float(self.every_minutes(spec)) * 60, observation_keep_seconds(a) * 2)
        if hit and not force and self.clock() - hit[0] < wait * 0.9:
            return bridge_as_of(name, hit[1], datetime.fromtimestamp(self.clock(), timezone.utc))
        out = dict(bridge.read_account(self.cfg, a['read_on'], name, force))
        out['keep_seconds'] = observation_keep_seconds(a)
        self.runs += 1
        observed = self.clock()
        if out.get('used') is not None:
            try:
                if type(out.get('observation_version')) is not int or out['observation_version'] != 1:
                    raise ValueError('unsupported observation version')
                remote_at = pacing.parse_time(out['observed_at'])
                if remote_at > datetime.fromtimestamp(observed, timezone.utc):
                    raise ValueError('future observation')
            except (KeyError, TypeError, ValueError, OverflowError):
                out.update(used=None, observed_at=None,
                           problem=f'accounts.{name}: bridge lacks a valid observation time; update the bridge and retry')
        self.cache[key] = (observed, out, None)
        return bridge_as_of(name, out, datetime.fromtimestamp(self.clock(), timezone.utc))

    def read(self, name, a, now=None, force=False):
        """{'plan', 'used', 'resets', 'problem', 'every_minutes'} for one account from its `reader` and `plan` commands."""
        if a.get('read_on'):
            return self.read_there(name, a, force, now)
        problem = remote_codex_problem(self.cfg, name, a)
        if problem:
            return {'plan': None, 'used': None, 'resets': None, 'every_minutes': 15,
                    'problem': problem}
        out = {'plan': None, 'used': None, 'resets': None, 'problem': None, 'every_minutes': None,
               'keep_seconds': observation_keep_seconds(a)}
        if a.get('plan'):
            data, problem = self._answer(a['plan'], force, now)
            try:
                out['plan'] = json_field(data, a['plan']['field']) if data is not None else None
            except ValueError as e:
                problem = f'accounts.{name}.plan: {e}'
            out['problem'] = problem
        r = automatic_reader(name, a)
        if r:
            spec = account_reader_spec(r, name, a)
            data, problem = self._answer(spec, force, now, observation_keep_seconds(a) * 2)
            out['observed_at'] = self.cache[key_of(spec)][3]
            out['observation_version'] = 1
            out['every_minutes'] = self.interval_minutes(spec)
            if data is not None:
                try:
                    if spec.get('format') == 'browser-usage':
                        value = (data.get('readings') or {}).get(name)
                        if not value or value.get('used') is None:
                            raise ValueError(f'no reading for {name}: check the usage page labels in accounts.{name}.helper_label')
                        used = float(value['used'])
                        resets = pacing.parse_time(value['resets']) if value.get('resets') else None
                        out['used'], out['resets'] = used, resets
                    else:
                        out['used'], out['resets'] = self.parse(r, a, data, now or datetime.fromtimestamp(self.clock(), timezone.utc))
                except (ValueError, KeyError, TypeError) as e:
                    problem = f'accounts.{name}.reader: {e}'
                    self.backoff[key_of(spec)] = 1   # an answer it cannot read is asked again at its own interval
            out['problem'] = problem or out['problem']
        return out

    @staticmethod
    def parse(r, a, data, now):
        """(used %, reset) from a reader's answer: GitHub's premium-request report, else agy's /usage buckets."""
        if r.get('format') == 'codex-app-server':
            used, resets = codexusage.parse(data, a, r)
        elif r.get('format') == 'github-premium':
            used, resets = github_premium(data, r['allowance'], now)
        elif r.get('format') == 'cursor-api':
            used, resets = cursorusage.parse(data, r['field'])
            if resets is None:
                return used, None
        else:
            used, resets = agy_usage(data, r['bucket'])
        if resets is None:
            return used, None
        return used, not_started(resets, a.get('window'), now)

    def read_all(self, now=None, force=False):
        """Every account with a reader or plan. Pass the `now` the readings will be recorded at."""
        accounts = {n: a or {} for n, a in self.cfg['accounts'].items()}
        out = {}
        for n, a in accounts.items():
            if not (automatic_reader(n, a) or a.get('plan')):
                continue
            try:
                out[n] = self.read(n, a, now, force)
            except Exception as e:   # one broken account never stops the others' readings
                out[n] = {'plan': None, 'used': None, 'resets': None, 'every_minutes': None,
                          'problem': f'accounts.{n}: {type(e).__name__}: {e}'}
        return out

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


def worth_keeping(last, used, resets, now, keep_every=KEEP_EVERY):
    if not last:
        return True
    if abs(float(last['used']) - used) >= MIN_CHANGE or now - pacing.parse_time(last['at']) >= keep_every:
        return True
    old = last.get('resets')
    return bool(used > 0 and ((not old and resets) or (old and resets and abs(pacing.parse_time(old) - resets) >= RESET_MOVED)))


def overrides(newest, used):
    """A reader's reading replaces a newer entry of another source (a limit message) that says something else: the CLI's own
    answer is the truth now, so an old "spent" from a job's output must not outlive it."""
    return bool(newest) and newest.get('source') == 'limit message' and abs(float(newest['used']) - used) >= MIN_CHANGE


SAME_RESET = timedelta(minutes=70)   # agy says "in 4 days, 13 hours": hour precision, so up to an hour off, plus slack


def owner_by_reset(name, reset, readings, now=None):
    """The account a limit message really belongs to: agy's message names no model, but its reset is the spent cap's own. When
    the matched account's reading resets elsewhere (in the future) and another reading resets then (a per-model cap such as
    Gemini 4 Argon's weekly), the message goes to the one whose reset is closest. Readings whose reset is past say nothing."""
    def gap(r):
        live = r and r.get('resets') and (now is None or r['resets'] > now)
        return abs(r['resets'] - reset) if live else None
    if not reset:
        return name
    own = gap(readings.get(name))
    if own is None or own <= SAME_RESET:
        return name
    near = sorted((g, n) for n, r in readings.items() if n != name and (g := gap(r)) is not None and g <= SAME_RESET)
    return near[0][1] if near else name


def last_resets(entries):
    """{account: {'resets'}} from each account's newest reader entry: a run's limit message is placed without a fresh reading."""
    out = {}
    for e in entries:
        if e.get('source') == 'reader' and e.get('resets') and e['at'] >= out.get(e['account'], {}).get('at', ''):
            out[e['account']] = {'at': e['at'], 'resets': pacing.parse_time(e['resets'])}
    return out


def observation_time(reading, now):
    """Return a trustworthy non-future observation time, or None; unversioned direct callers retain legacy behavior."""
    version = reading.get('observation_version')
    if version is not None and (type(version) is not int or version != 1 or not reading.get('observed_at')):
        return None
    try:
        observed = pacing.parse_time(reading['observed_at']) if reading.get('observed_at') else now
        return observed if observed <= now else None
    except (TypeError, ValueError, OverflowError):
        return None


def record(path, readings, hits, now, hit_now=None):
    """Appends the readers' readings that say something new and the limit messages not logged yet. Returns the lines written."""
    entries = pacing.read_log(path)
    refs = {e.get('ref') for e in entries if e.get('ref')}
    written = []
    for name, r in readings.items():
        if r.get('used') is None:
            continue
        observed = observation_time(r, now)
        if observed is None:
            continue
        last = _last_auto(entries, name)
        if last and observed <= pacing.parse_time(last['at']):
            continue   # a cached or older observation is not a new provider read
        newest = pacing.latest(entries, name)
        newer_than_newest = not newest or observed > pacing.parse_time(newest['at'])
        keep_seconds = r.get('keep_seconds', KEEP_EVERY.total_seconds())
        if (isinstance(keep_seconds, bool) or not isinstance(keep_seconds, (int, float)) or
                not math.isfinite(keep_seconds) or not 0 < keep_seconds <= KEEP_EVERY.total_seconds()):
            continue
        if not (worth_keeping(last, r['used'], r['resets'], observed, timedelta(seconds=keep_seconds)) or
                (newer_than_newest and overrides(newest, r['used']))):
            continue
        written.append(pacing.append_log(path, name, r['used'], observed, 'read automatically', resets=r['resets'], source='reader'))
    for name, reset, ref, _text in hits:
        if ref in refs:
            continue
        at = hit_now or now
        name = owner_by_reset(name, reset, {**last_resets(entries), **readings}, at)
        refs.add(ref)
        note = f'a limit message in {ref}' + ('' if reset else ' (no reset time in it: the window rule applies)')
        written.append(pacing.append_log(path, name, 100, at, note, resets=reset, source='limit message', ref=ref))
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


def read_file_of(cfg, name, a):
    """(path, mtime, text reader) of an account's reading file: here, or on the computer named by accounts.<n>.reading_from
    (its orchestrator writes it there; `tutti move` sets the key), read through that computer's bridge."""
    f = config.expand(a.get('reading_file') or '') if a.get('reading_file') else None
    if not f:
        return None, None, None
    if a.get('reading_from') and cfg is not None:
        from . import bridge
        text, seen = bridge.reading_file(cfg, a['reading_from'], name)
        return f, seen, (lambda: text)

    def text():
        with open(f, encoding='utf8') as fh:
            return fh.read()
    return f, mtime(f), text


def file_status(name, used, resets, at, prior):
    """A reused file reference is safe only while its parsed observation is unchanged."""
    if prior is None:
        return {'used': used, 'problem': None}
    try:
        same_reset = (pacing.parse_time(prior['resets']) if prior.get('resets') else None) == resets
        same_time = at is None or pacing.parse_time(prior['at']) == at
        unchanged = prior.get('used') == used and same_reset and same_time
    except (KeyError, TypeError, ValueError, OverflowError):
        unchanged = False
    return {'used': used if unchanged else None, 'problem': None if unchanged else
            f'accounts.{name}.reading_file changed without a new modification time: rewrite it and refresh'}


def record_files(path, accounts, cfg=None, status=None):
    """Readings an orchestrator wrote to accounts.<name>.reading_file (it gets the plan's % from its own tool, which no CLI
    prints). Each version of the file is logged once (its ref holds the file's mtime). Returns (lines written, problems)."""
    entries = pacing.read_log(path)
    refs = {e.get('ref') for e in entries if e.get('ref')}
    by_ref = {e.get('ref'): e for e in entries if e.get('ref')}
    written, problems = [], []
    for name, a in accounts.items():
        f, seen, text = read_file_of(cfg, name, a or {})
        if not f:
            continue
        ref = f'file:{f}:{name}:{seen}'   # one file may hold several accounts: each is logged once per version
        if seen is None:
            if status is not None:
                status[name] = {'used': None, 'problem': f'accounts.{name}.reading_file is unavailable: check its path and reader'}
            continue
        if ref in refs and status is None:
            continue
        try:
            used, resets, at = parse_reading_file(text(), name)
        except (OSError, ValueError, TypeError) as e:
            problem = f'accounts.{name}.reading_file {f}: {e}: write a percentage such as 42, or {{"used": 42}}'
            problems.append(problem)
            if status is not None:
                status[name] = {'used': None, 'problem': problem}
            continue
        if status is not None:
            status[name] = file_status(name, used, resets, at, by_ref.get(ref))
        if ref in refs:
            continue
        written.append(pacing.append_log(path, name, used, at or datetime.fromtimestamp(seen, timezone.utc), 'from the reading file',
                                         resets=resets, source='file', ref=ref))
    return written, problems


def utcnow():
    return datetime.now(timezone.utc)
