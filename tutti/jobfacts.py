"""What a job has done so far, or did: tests passed and failed, tokens, AI credits, the account it spends, files changed,
bytes copied, the current step, its result and duration. Read from the end of its log, a delegated run's report and an
agent's transcript. Counts only: no text of a log or a transcript is ever copied into the facts."""
import json
import os
import re
import subprocess
import time

from . import readers

# Test counts, as test runners print them: pytest "12 passed, 1 failed", vitest "Tests  12 passed | 1 failed (13)",
# Playwright "  12 passed (1.2m)" and "  1 failed", unittest "Ran 12 tests" then "FAILED (failures=1, errors=2)".
PASSED = re.compile(r'\b(\d+)\s+passed\b', re.I)
FAILED = re.compile(r'\b(\d+)\s+failed\b', re.I)
UNITTEST_RAN = re.compile(r'^Ran (\d+) tests? in ')
UNITTEST_BAD = re.compile(r'^FAILED \((.*)\)')
# Tokens, as agent CLIs print them in text: "12,345 input tokens", "Tokens: 12.3k in, 4.1k out", "input_tokens": 123.
NUM = r'(\d[\d,]*(?:\.\d+)?\s*[kKmM]?)'
TOKENS_IN = re.compile(NUM + r'\s+(?:input|prompt)\s+tokens|"input_tokens"\s*:\s*(\d+)', re.I)
TOKENS_OUT = re.compile(NUM + r'\s+(?:output|completion)\s+tokens|"output_tokens"\s*:\s*(\d+)', re.I)
TOKENS_PAIR = re.compile(r'\btokens?\b\W{0,4}' + NUM + r'\s+in\b\W{0,4}' + NUM + r'\s+out\b', re.I)
SHORTSTAT = re.compile(r'(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?')
TRANSCRIPT_MAX = 8 * 1024 * 1024      # an agent transcript larger than this is read from its last 8 MB
DIFF_TTL = 30                          # a running run's `git diff --shortstat` is asked at most every 30 s
_cache = {}


def number(text):
    """'12,345' -> 12345, '12.3k' -> 12300, '1.2M' -> 1200000; None when it is not a number."""
    t = (text or '').replace(',', '').replace(' ', '')
    scale = {'k': 1e3, 'm': 1e6}.get(t[-1:].lower(), 1)
    try:
        return int(float(t.rstrip('kKmM')) * scale)
    except ValueError:
        return None


def tests(lines):
    """{passed, failed} from the last lines that count tests, or None when no line does."""
    passed = failed = None
    for line in reversed(lines):
        ran, bad = UNITTEST_RAN.search(line), UNITTEST_BAD.search(line)
        if bad and failed is None:
            failed = sum(int(n) for n in re.findall(r'=(\d+)', bad.group(1)))
        if ran and passed is None:
            passed = int(ran.group(1)) - (failed or 0)
        if passed is None and PASSED.search(line):
            passed = int(PASSED.search(line).group(1))
        if failed is None and FAILED.search(line) and not line.lstrip().startswith(('FAILED ', 'FAIL ')):
            failed = int(FAILED.search(line).group(1))
        if passed is not None and failed is not None:
            break
    if passed is None and failed is None:
        return None
    return {'passed': passed or 0, 'failed': failed or 0}


def _last(pattern, lines):
    for line in reversed(lines):
        m = pattern.search(line)
        if m:
            return number(next(g for g in m.groups() if g))
    return None


def tokens_in_lines(lines):
    """{in, out} from the last token counts an agent CLI printed, or None."""
    for line in reversed(lines):
        m = TOKENS_PAIR.search(line)
        if m:
            return {'in': number(m.group(1)), 'out': number(m.group(2))}
    tin, tout = _last(TOKENS_IN, lines), _last(TOKENS_OUT, lines)
    if tin is None and tout is None:
        return None
    return {'in': tin, 'out': tout}


def transcript_tokens(path):
    """{in, out, calls} of an agent session's JSONL transcript (a Claude Code subagent's task output): per message the last
    usage seen (a streamed message repeats it), in = input + cache tokens, calls = tool calls. Cached by size and time."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = ('transcript', path)
    hit = _cache.get(key)
    if hit and hit[0] == (st.st_size, st.st_mtime):
        return hit[1]
    usage, calls = {}, 0
    try:
        with open(path, 'rb') as f:
            f.seek(max(0, st.st_size - TRANSCRIPT_MAX))
            for raw in f:
                calls += raw.count(b'"type":"tool_use"')
                if b'"usage"' not in raw:
                    continue
                try:
                    msg = (json.loads(raw).get('message') or {})
                except (ValueError, AttributeError):
                    continue
                u = msg.get('usage') if isinstance(msg, dict) else None
                if isinstance(u, dict):
                    usage[msg.get('id') or len(usage)] = u
    except OSError:
        return None
    out = None
    if usage:
        keys = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
        tin = sum(int(u.get(k) or 0) for u in usage.values() for k in keys)
        out = {'in': tin, 'out': sum(int(u.get('output_tokens') or 0) for u in usage.values()), 'calls': calls}
    _cache[key] = ((st.st_size, st.st_mtime), out)
    return out


def is_transcript(path):
    """True when a task output is an agent's JSONL transcript (its first byte opens a JSON object), not a command's output."""
    try:
        with open(path, 'rb') as f:
            return f.read(1) == b'{'
    except (OSError, TypeError):
        return False


TIMESTAMP = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')


def transcript_started(path):
    """When an agent's transcript began: the first "timestamp" of its first lines, or None."""
    key = ('started', path)
    if key in _cache:
        return _cache[key]
    found = None
    try:
        with open(path, encoding='utf8', errors='replace') as f:
            for _ in range(5):
                m = TIMESTAMP.search(f.readline())
                if m:
                    found = seconds(m.group(1).replace('Z', '+00:00'))
                    break
    except OSError:
        return None
    if found:
        _cache[key] = found
    return found


def files_in_lines(lines):
    """{files, plus, minus} from the last git "N files changed, X insertions(+), Y deletions(-)" line, or None."""
    for line in reversed(lines):
        m = SHORTSTAT.search(line)
        if m:
            return {'files': int(m.group(1)), 'plus': int(m.group(2) or 0), 'minus': int(m.group(3) or 0)}
    return None


def diff_files(worktree, clock=time.time, run=subprocess.run):
    """A running delegated run's changes so far: `git diff --shortstat HEAD` in its worktree, at most every DIFF_TTL s."""
    key = ('diff', worktree)
    hit = _cache.get(key)
    if hit and clock() - hit[0] < DIFF_TTL:
        return hit[1]
    try:
        out = run(['git', '-C', worktree, 'diff', '--shortstat', 'HEAD'], capture_output=True, text=True, timeout=5).stdout
        found = files_in_lines([out]) or ({'files': 0, 'plus': 0, 'minus': 0} if os.path.isdir(worktree) else None)
    except (OSError, subprocess.SubprocessError):
        found = None
    _cache[key] = (clock(), found)
    return found


def read_json(path):
    try:
        with open(path, encoding='utf8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run_dir(job):
    """The folder of a delegated run (<state>/runs/<id>) from its log <state>/runs/<id>.log."""
    return job['log'][:-len('.log')] if job.get('kind') == 'run' and job.get('log') else None


def run_entry(state_folder, run_id):
    """The runs.jsonl line of a finished delegated run: its worker, account and credits."""
    try:
        with open(os.path.join(state_folder, 'runs.jsonl'), encoding='utf8') as f:
            for line in f:
                if run_id in line:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get('id') == run_id:
                        return e
    except OSError:
        pass
    return None


def run_facts(job, state_folder, worker_account):
    """A delegated run: its worker and account, files changed (the report, or the worktree so far), the report's path."""
    d = run_dir(job)
    out = {}
    report = read_json(os.path.join(d, 'report.json')) or {}
    meta = read_json(os.path.join(d, 'meta.json')) or {}
    m = re.match(r'run (\S+):', job.get('title') or '')
    worker = report.get('worker') or (m.group(1) if m else None)
    entry = run_entry(state_folder, os.path.basename(d)) or {}
    if worker:
        out['worker'] = worker
    account = entry.get('account') or worker_account(worker)
    if account:
        out['account'] = account
    if entry.get('credits'):
        out['credits'] = entry['credits']
    if report:
        out['files'] = files_in_lines([report.get('stat') or '']) or {'files': len(report.get('changed') or []), 'plus': 0, 'minus': 0}
        out['report'] = report.get('report')
        out['checks'] = {'passed': sum(1 for c in report.get('checks') or [] if c.get('rc') == 0),
                         'failed': sum(1 for c in report.get('checks') or [] if c.get('rc') != 0)}
    elif meta.get('worktree') and job.get('status') in ('running', 'stalled'):
        out['files'] = diff_files(meta['worktree'])
    return {k: v for k, v in out.items() if v is not None}


def seconds(iso_text):
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso_text).timestamp()
    except (TypeError, ValueError):
        return None


def timing(job, now):
    """Elapsed seconds (running) or the duration (finished), the result and the exit code."""
    out = {}
    started = seconds(job.get('started')) or job.get('first_seen')
    if started:
        end = now if job['status'] in ('running', 'stalled') else (seconds(job.get('updated')) or now)
        out['seconds'] = max(0, round(end - started))
    if job['status'] in ('done', 'failed', 'ended'):
        out['result'] = {'done': 'passed', 'failed': 'failed'}.get(job['status'], 'ended')
        if job.get('rc') is not None:
            out['rc'] = job['rc']
    return out


def from_lines(job):
    """Tests, tokens, AI credits and files changed, as the end of the log (or an agent's transcript) says them."""
    lines = job.get('tail') or []
    tok = transcript_tokens(job['path']) if job.get('kind') == 'agent task' and job.get('path') else None
    found = {'tests': tests(lines), 'tokens': tok or tokens_in_lines(lines), 'credits': readers.credits_in(lines) or None,
             'files': files_in_lines(lines)}
    return {k: v for k, v in found.items() if v}


def facts(job, state_folder, worker_account, now):
    """The job's facts: always its elapsed time (or duration when finished), then whatever its log, report or transcript says."""
    out = {**timing(job, now), **from_lines(job)}
    p = job.get('progress') or {}
    if p.get('unit'):
        out['copied'] = {'done': p['done'], 'total': p['total'], 'unit': p['unit']}
    if job.get('kind') == 'run' and job.get('log'):
        out.update(run_facts(job, state_folder, worker_account))
    if job.get('log'):
        out['log'] = job['log']
    return out
