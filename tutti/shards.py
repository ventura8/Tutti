"""A suite's finer points for Keep busy (docs/DISPATCH.md, *Suites in depth*): a shard's own options (the devices it may run
on, the extra arguments its command gets as {extra}, the other devices it holds while it runs), the build that runs once per
commit before any shard (`prepare`), how a run's output is judged (`verdict`, `started`, `requeue_on`) and the report a
project's results are written to (`projects.<p>.report`). Pure, apart from the build's spawn and the report's write."""
import os
import re
import shlex
import subprocess
import sys
import time

REQUEUE = 'requeue:'          # an rc file that starts with this queues the shard again, the reason after it (not a failure)
PREPARE_RETRY = 1800          # seconds before a failed build is tried again on the same commit
TAIL = 40                     # log lines a failed shard shows in the report


# ---- the config


def options(s):
    """{shard name: {devices, extra, holds}} from a suite's `shards:` list, whose items are names or mappings with `name`."""
    return {str(x['name']): x for x in s.get('shards') or [] if isinstance(x, dict) and 'name' in x}


def names(s):
    return [str(x['name']) if isinstance(x, dict) else str(x) for x in s.get('shards') or [] if not isinstance(x, dict) or 'name' in x]


def _regex(key, v):
    try:
        re.compile(v)
    except (re.error, TypeError):
        return [f'{key} is {v!r}: a regular expression, e.g. "\\\\[E2E\\\\] FAIL"']
    return []


def _shard_errors(cfg, key, x):
    if isinstance(x, (str, int)) and not isinstance(x, bool):
        return []
    if not isinstance(x, dict) or not isinstance(x.get('name'), (str, int)):
        return [f'{key} is {x!r}: a shard name, or {{name: <name>, devices: [...], extra: <args>, holds: [<device>]}}']
    errs = [f'{key}.{k} names "{n}", which is not under devices:' for k in ('devices', 'holds') for n in x.get(k) or []
            if n not in (cfg.get('devices') or {})]
    if 'extra' in x and not isinstance(x['extra'], str):
        errs.append(f'{key}.extra is {x["extra"]!r}: the extra arguments as one string (the command\'s {{extra}})')
    return errs


def suite_errors(cfg, key, s, where_errors):
    errs = []
    for i, x in enumerate(s.get('shards') or []):
        errs += _shard_errors(cfg, f'{key}.shards[{i}]', x)
    if 'prepare' in s:
        errs += where_errors(cfg, f'{key}.prepare', s['prepare'])
    v = s.get('verdict')
    if v is not None:
        if not isinstance(v, dict) or not set(v) <= {'pass', 'fail'}:
            errs.append(f'{key}.verdict is {v!r}: write it as {{pass: <regex a passing run prints>, fail: <regex of a failure>}}')
        else:
            errs += [e for k in v for e in _regex(f'{key}.verdict.{k}', v[k])]
    st = s.get('started')
    if st is not None:
        ok = isinstance(st, dict) and isinstance(st.get('within_seconds'), (int, float)) and st['within_seconds'] > 0
        errs += _regex(f'{key}.started.pattern', st.get('pattern')) if ok else \
            [f'{key}.started is {st!r}: write it as {{pattern: <regex of the line that shows tests run>, within_seconds: 120}}']
    for i, r in enumerate(s.get('requeue_on') or []):
        errs += _regex(f'{key}.requeue_on[{i}]', r)
    return errs


def report_errors(cfg, key, r):
    if not isinstance(r, dict) or not isinstance(r.get('path'), str) or not r['path'].strip():
        return [f'{key} is {r!r}: write it as {{path: <a file on the machine>, where: <a machine>}}']
    if r.get('where', 'local') != 'local' and r['where'] not in cfg['machines']:
        return [f'{key}.where names "{r["where"]}", which is not under machines: (or use local)']
    return []


# ---- judging a run's output


def started_marker():
    return 'tutti: device ready'


def judge(s, log, rc):
    """The run's result from its output: an int exit code, or REQUEUE + why. requeue_on wins (not the test's fault), then
    the suite's verdict (a runner that always exits 0), else the exit code."""
    for r in s.get('requeue_on') or []:
        m = re.search(r, log)
        if m:
            return f'{REQUEUE} the run printed "{m.group(0)[:120]}": queued again, not a failure'
    v = s.get('verdict') or {}
    if rc not in (0, None) or not v:
        return rc
    if v.get('fail') and re.search(v['fail'], log):
        return 1
    if v.get('pass') and not re.search(v['pass'], log):
        return 1
    return 0


def not_started(s, log, since_start):
    """Why the run must stop because its tests never started, or ''. The clock runs from the device lock's marker line."""
    st = s.get('started')
    if not st or since_start < float(st['within_seconds']) or re.search(st['pattern'], log):
        return ''
    return (f'{REQUEUE} no test started within {int(st["within_seconds"])} s (no "{st["pattern"]}" line): the device may be '
            'locked or in use; queued again, not a failure')


# ---- the build that runs once per commit


def prepare_key(lease_or_w):
    return lease_or_w['suite'] if 'suite' in lease_or_w else lease_or_w['key'].partition('/')[0]


def prepare_wait(p, s, now):
    """Why the suite's shards wait for its build on this commit ('' when it passed or there is none)."""
    if not s.get('prepare'):
        return ''
    b = (p.get('prepare') or {}).get(s['name']) or {}
    if not b or b.get('commit') != p.get('commit'):
        return f'{s["project"]} {s["name"]}: the build for this commit has not started yet'
    if b.get('status') == 'running':
        return f'{s["project"]} {s["name"]}: the build for this commit is running'
    if b.get('status') == 'failed':
        again = time.strftime('%H:%M', time.localtime(b['at'] + PREPARE_RETRY))
        return f'{s["project"]} {s["name"]}: the build failed ({b.get("why")}); it is tried again at {again} (or on a new commit)'
    return ''


def prepare_due(p, s, now):
    b = (p.get('prepare') or {}).get(s['name']) or {}
    if not s.get('prepare') or b.get('status') == 'running':
        return False
    if not b or b.get('commit') != p.get('commit'):
        return True
    return b.get('status') == 'failed' and now - b.get('at', 0) >= PREPARE_RETRY


def start_prepare(cfg, p, s, folder, now, spawn=subprocess.Popen):
    from . import remote
    log = os.path.join(folder, f'build-{_slug(s["project"])}-{_slug(s["name"])}.log')
    rc = log[:-4] + '.rc'
    if os.path.exists(rc):
        os.remove(rc)
    with open(log, 'w', encoding='utf8') as f:
        f.write(f'dispatch: build of {s["project"]} {s["name"]} at {p.get("commit")}\n')
        child = spawn([sys.executable, remote.BIN, '--config', cfg['_path'], 'dispatch', 'build', s['project'], s['name'], rc],
                      stdout=f, stderr=f, stdin=subprocess.DEVNULL, start_new_session=True)
    p.setdefault('prepare', {})[s['name']] = {'commit': p.get('commit'), 'status': 'running', 'pid': child.pid, 'at': now,
                                              'log': log, 'rc': rc}


def collect_prepare(p, now, is_alive):
    """Ends finished or dead builds; returns lines for the events."""
    out = []
    for name, b in (p.get('prepare') or {}).items():
        if b.get('status') != 'running':
            continue
        try:
            with open(b['rc'], encoding='utf8') as f:
                rc = int(f.read().strip())
        except (OSError, ValueError):
            if is_alive(b['pid']):
                continue
            rc = None
        ok = rc == 0
        why = 'built' if ok else ('the build died without a result' if rc is None else f'exit {rc}: see {b["log"]}')
        b.update(status='passed' if ok else 'failed', why=why, at=now)
        out.append(f'build of {name}: {why}')
    return out


def run_prepare(cfg, project, suite, rc_path):
    """`tutti dispatch build <project> <suite> <rc>`: the suite's prepare command on its machine; writes the exit code."""
    from . import dispatch, remote
    s = next(x for x in dispatch.suites(cfg) if x['project'] == project and x['name'] == suite)
    spec = s['prepare']
    machine = {'transport': 'local', 'name': 'local'} if spec.get('where', 'local') == 'local' else cfg['machines'][spec['where']]
    rc = 2
    try:
        rc = remote.sh(machine, spec['run']).returncode
    finally:
        with open(rc_path, 'w', encoding='utf8') as f:
            f.write(f'{rc}\n')
    return rc


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '-', str(text).lower()).strip('-')


# ---- the report a project's results go to


def tail(path, lines=TAIL):
    try:
        with open(path, encoding='utf8', errors='replace') as f:
            return f.read().splitlines()[-lines:]
    except OSError:
        return []


def report(project, p, now):
    """The project's results on its latest commit as Markdown: counts, then every failed shard with why and its log's end."""
    shards = p.get('shards') or {}
    counts = {}
    for sh in shards.values():
        counts[sh['status']] = counts.get(sh['status'], 0) + 1
    lines = [f'# {project}: Tutti test runs', '',
             f'Commit `{(p.get("commit") or "")[:12]}`, updated {time.strftime("%Y-%m-%d %H:%M", time.localtime(now))}: ' +
             (', '.join(f'{n} {k}' for k, n in sorted(counts.items())) or 'no shards'), '']
    for name, b in sorted((p.get('prepare') or {}).items()):
        lines.append(f'- build of {name}: {b.get("status")} ({b.get("why") or "running"})')
    failed = sorted((k, sh) for k, sh in shards.items() if sh['status'] == 'failed')
    if failed:
        lines += ['', '## Failed', '']
    for key, sh in failed:
        lines += [f'### {key} ({sh["attempts"]} attempt(s), on {sh.get("target")})', '', sh.get('why') or '', '', '```',
                  *tail(sh.get('log') or ''), '```', '']
    return '\n'.join(lines) + '\n'


def write_report(cfg, project, p, now):
    """Writes the report where `projects.<p>.report` says (a file on that machine); False when it could not."""
    from . import remote
    r = (cfg['projects'].get(project) or {}).get('report')
    if not r:
        return False
    machine = {'transport': 'local', 'name': 'local'} if r.get('where', 'local') == 'local' else cfg['machines'][r['where']]
    path = shlex.quote(r['path'])
    try:
        r = remote.sh(machine, f'mkdir -p "$(dirname {path})" && cat > {path}', stdin=report(project, p, now), timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
