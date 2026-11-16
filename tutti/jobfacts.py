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
SHORTSTAT = re.compile(r'(\d+) files? changed')
INSERTIONS = re.compile(r'(\d+) insertions?\(\+\)')
DELETIONS = re.compile(r'(\d+) deletions?\(-\)')
TRANSCRIPT_MAX = 8 * 1024 * 1024      # an agent transcript larger than this is read from its last 8 MB
UNTRACKED_MAX = 200                    # untracked files counted in a running run's changes
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


def _line_counts(line):
    """(passed, failed) one line says, each None when it does not say it."""
    bad, ran = UNITTEST_BAD.search(line), UNITTEST_RAN.search(line)
    if bad:
        return None, sum(int(n) for n in re.findall(r'=(\d+)', bad.group(1)))
    if ran:
        return int(ran.group(1)), None
    p, f = PASSED.search(line), FAILED.search(line)
    if line.lstrip().startswith(('FAILED ', 'FAIL ')):
        f = None   # a failing test's own line, not a count
    return (int(p.group(1)) if p else None), (int(f.group(1)) if f else None)


def tests(lines):
    """{passed, failed} from the last lines that count tests, or None when no line does. unittest's "Ran N tests" is the
    total: the failures and errors of its "FAILED (...)" line are taken out of it."""
    passed = failed = None
    ran_total = False
    for line in reversed(lines):
        p, f = _line_counts(line)
        if failed is None and f is not None:
            failed = f
        if passed is None and p is not None:
            passed, ran_total = p, bool(UNITTEST_RAN.search(line))
        if passed is not None and failed is not None:
            break
    if passed is None and failed is None:
        return None
    if ran_total:
        passed -= failed or 0
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


def usage_of(raw):
    """(message id, usage) of one transcript line, or None."""
    try:
        data = json.loads(raw)
    except (ValueError, AttributeError):
        return None
    if not isinstance(data, dict):
        return None
    msg = data.get('message')
    if isinstance(msg, dict) and isinstance(msg.get('usage'), dict):
        return msg.get('id'), msg['usage']
    if 'input_tokens' in data:
        return data.get('step_index') or len(data), data
    return None


def read_usage(path, size):
    """({message id: its last usage}, tool calls) from the last TRANSCRIPT_MAX bytes of a transcript."""
    usage, calls = {}, 0
    with open(path, 'rb') as f:
        f.seek(max(0, size - TRANSCRIPT_MAX))
        for raw in f:
            calls += raw.count(b'"type":"tool_use"')
            found = usage_of(raw) if (b'"usage"' in raw or b'"input_tokens"' in raw) else None
            if found:
                usage[found[0] or len(usage)] = found[1]
    return usage, calls


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
    try:
        usage, calls = read_usage(path, st.st_size)
    except OSError:
        return None
    out = None
    if usage:
        keys = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'cache_read_tokens')
        tin = sum(int(u.get(k) or 0) for u in usage.values() for k in keys)
        last = list(usage.values())[-1]   # the newest message's prompt is the context the agent holds now
        out = {'in': tin, 'out': sum(int(u.get('output_tokens') or 0) for u in usage.values()), 'calls': calls,
               'context': sum(int(last.get(k) or 0) for k in keys)}
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
            plus, minus = INSERTIONS.search(line), DELETIONS.search(line)
            return {'files': int(m.group(1)), 'plus': int(plus.group(1)) if plus else 0, 'minus': int(minus.group(1)) if minus else 0}
    return None


def new_files(worktree, run):
    """(files, lines) of the untracked files a worker added (git ignores them in `diff HEAD`), at most UNTRACKED_MAX files
    and the first MB of each counted; the index is never touched."""
    out = run(['git', '-C', worktree, 'ls-files', '--others', '--exclude-standard', '-z'], capture_output=True, timeout=5).stdout
    names = [n for n in out.split(b'\0') if n][:UNTRACKED_MAX]
    lines = 0
    for n in names:
        try:
            with open(os.path.join(worktree, os.fsdecode(n)), 'rb') as f:
                lines += f.read(1024 * 1024).count(b'\n')
        except OSError:
            continue
    return len(names), lines


def diff_files(worktree, clock=time.time, run=subprocess.run):
    """A running delegated run's changes so far: `git diff --shortstat HEAD` (tracked files) plus the files it added,
    at most every DIFF_TTL s."""
    key = ('diff', worktree)
    hit = _cache.get(key)
    if hit and clock() - hit[0] < DIFF_TTL:
        return hit[1]
    try:
        out = run(['git', '-C', worktree, 'diff', '--shortstat', 'HEAD'], capture_output=True, text=True, timeout=5).stdout
        found = files_in_lines([out]) or {'files': 0, 'plus': 0, 'minus': 0}
        added, lines = new_files(worktree, run)
        found = {**found, 'files': found['files'] + added, 'plus': found['plus'] + lines}
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


def report_facts(report):
    """Files changed, checks passed and failed, and the report's path, from a finished run's report.json."""
    checks = report.get('checks') or []
    files = files_in_lines([report.get('stat') or '']) or {'files': len(report.get('changed') or []), 'plus': 0, 'minus': 0}
    return {'files': files, 'report': report.get('report'),
            'checks': {'passed': sum(1 for c in checks if c.get('rc') == 0), 'failed': sum(1 for c in checks if c.get('rc') != 0)}}


def run_facts(job, state_folder, worker_account, own_tree=lambda path: None):
    """A delegated run: its worker and account, files changed (the report, or the worktree so far), the report's path.
    `own_tree(path)` is the worktree when it is one of Tutti's run worktrees, else None: a meta.json edited to name another
    folder never gets git run in it."""
    d = run_dir(job)
    report = read_json(os.path.join(d, 'report.json')) or {}
    m = re.match(r'run (\S+):', job.get('title') or '')
    worker = report.get('worker') or (m.group(1) if m else None)
    entry = run_entry(state_folder, os.path.basename(d)) or {}
    out = {'worker': worker, 'account': entry.get('account') or worker_account(worker), 'credits': entry.get('credits') or None}
    if report:
        out.update(report_facts(report))
    elif job.get('status') in ('running', 'stalled'):
        tree = own_tree((read_json(os.path.join(d, 'meta.json')) or {}).get('worktree'))
        out['files'] = diff_files(tree) if tree else None
    return {k: v for k, v in out.items() if v is not None}


def seconds(iso_text):
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso_text).timestamp()
    except (TypeError, ValueError):
        return None


def state_tail(path):
    from . import state  # state imports this module's callers
    return state.tail(path)


def timing(job, now):
    """Elapsed seconds (running) or the duration (finished), the result and the exit code."""
    out = {}
    started = seconds(job.get('started')) or job.get('first_seen')
    # a finished job's duration only when its end is known (its result's time), never the last output's time
    end = now if job['status'] in ('running', 'stalled') else seconds(job.get('ended'))
    if started and end:
        out['seconds'] = max(0, round(end - started))
    if job['status'] in ('done', 'failed', 'ended'):
        out['result'] = {'done': 'passed', 'failed': 'failed'}.get(job['status'], 'ended')
        if job.get('rc') is not None:
            out['rc'] = job['rc']
    return out


def from_lines(job):
    """Tests, tokens, AI credits and files changed, as the end of the log (or an agent's transcript) says them."""
    lines = job.get('tail') or []
    task = job['path'] if job.get('kind') == 'agent task' and job.get('path') else None
    tok = transcript_tokens(task) if task else None
    if task and not tok and not lines:
        lines = state_tail(task)   # a background command's output: read for counts only, never shown
    found = {'tests': tests(lines), 'tokens': tok or tokens_in_lines(lines), 'credits': readers.credits_in(lines) or None,
             'files': files_in_lines(lines)}
    return {k: v for k, v in found.items() if v}


def facts(job, state_folder, worker_account, now, own_tree=lambda path: None):
    """The job's facts: always its elapsed time (or duration when finished), then whatever its log, report or transcript says."""
    out = {**timing(job, now), **from_lines(job)}
    p = job.get('progress') or {}
    if p.get('unit'):
        out['copied'] = {'done': p['done'], 'total': p['total'], 'unit': p['unit']}
    if job.get('kind') == 'run' and job.get('log'):
        out.update(run_facts(job, state_folder, worker_account, own_tree))
    if job.get('log'):
        out['log'] = job['log']
    return out
