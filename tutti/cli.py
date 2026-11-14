"""The `tutti` command. Every subcommand reads tutti.config.yaml; nothing personal is built in."""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from . import (
    __version__,
    conduct,
    config,
    dashboard,
    delegate,
    discover,
    files,
    local,
    pacing,
    readers,
    remote,
    review,
    routing,
    ship,
    state,
    transfer,
    translate,
    waiting,
    workers,
)

SCRIPTS = os.path.join(remote.ROOT, 'scripts')
SUMMARY = 'In at most 5 short lines: which check failed, the file:line and the exact error. No advice.'


def say(msg):
    sys.stderr.write(f'tutti: {msg}\n')


def machine_of(cfg, name):
    if name not in cfg['machines']:
        raise config.ConfigError(f'no machine "{name}" in {cfg["_path"]}: the machines are {", ".join(cfg["machines"]) or "(none)"}')
    return cfg['machines'][name]


def worker_of(cfg, name, kind):
    w = cfg['workers'].get(name)
    if not w or w['kind'] != kind:
        names = [n for n, x in cfg['workers'].items() if x['kind'] == kind]
        have = ', '.join(names) or '(none)'
        raise config.ConfigError(f'"{name}" is not a {kind} worker in {cfg["_path"]}: the {kind} workers are {have}')
    return w


def lead_hours_warnings(cfg):
    """One line per account whose lead_hours is not the default 1 (the old example said 3), with the exact fix."""
    out = []
    for name, acc in cfg['accounts'].items():
        lead = (acc or {}).get('lead_hours', 1)
        if lead != 1:
            out.append(f'account "{name}" has lead_hours: {lead} (the old example\'s value; the default is 1 hour before the reset): '
                       f'set lead_hours: 1 for "{name}" in {cfg["_path"]}, or remove the key')
    return out


def cmd_config(cfg, a):
    print(f'{cfg["_path"]}: ok ({len(cfg["machines"])} machines, {len(cfg["workers"])} workers, {len(cfg["accounts"])} accounts)')
    for line in lead_hours_warnings(cfg):
        say(f'warning: {line}')
    return 0


def cmd_machines(cfg, a):
    for name, m in cfg['machines'].items():
        p = remote.probe(m)
        state = routing.unfit(m, p) or 'fit'
        heat = f'  {p.temp:.0f} C' if p.temp is not None else ''
        print(f'{name:12} {m["transport"]:5} slots {p.free}/{m["slots"]}  load {p.load:g}/{p.cores}{heat}  {state}')
    return 0


def cmd_test(cfg, a):
    if not a.command:
        say('name the command: tutti test <track> <command ...> (or one quoted string)')
        return 2
    src = config.expand(a.dir) if a.dir else os.path.join(config.expand(cfg['project']['worktrees']), a.track)
    if not os.path.isdir(src):
        say(f'no work tree at {src}: create the worktree for track "{a.track}" there, or pass --dir <path>')
        return 2
    if a.on:
        choice = routing.Choice(machine_of(cfg, a.on)['name'], False, 'chosen with --on')
    else:
        choice = routing.choose(cfg['routing']['order'], cfg['machines'], lambda n: remote.probe(cfg['machines'][n]))
    say(f'test: {choice.machine}{", queued" if choice.queued else ""} ({choice.reason})')
    machine = cfg['machines'][choice.machine]
    with job_inputs(cfg, machine, a.input) as base:
        return remote.run(cfg, machine, a.track, src, ' '.join(a.command), inputs=base and transfer.shell_path(base))


@contextmanager
def job_inputs(cfg, machine, paths):
    """A job's --input files, copied to the machine's cache first and pinned there until the job ends (no eviction, clean or
    replacing send touches them meanwhile). Yields the cache folder, or None without inputs."""
    if not paths:
        yield None
        return
    token = f'{os.getpid()}-{int(datetime.now(timezone.utc).timestamp())}'
    base, result = transfer.send(cfg, machine, [os.path.abspath(p) for p in paths], pin_token=token)
    say(f'inputs on {machine["name"]}: ' + ', '.join(f'{n} ({how})' for n, how, _ in result) + f' in {base} ($TUTTI_INPUTS)')
    try:
        yield base
    finally:
        transfer.unpin(machine, base, [n for n, _, _ in result], token)


def cmd_rsh(cfg, a):
    return remote.rsh_main(machine_of(cfg, a.machine), a.args)


def cmd_ship(cfg, a):
    if os.environ.get('TUTTI_CONDUCT') and (not a.dry_run or a.allow_protected):
        raise ship.ShipError(f'a main agent is conducting (run {os.environ["TUTTI_CONDUCT"]}): ship only with --dry-run and without '
                             '--allow-protected; a person ships after reading the plan')
    ship.ship(cfg, a.dir, a.message, full=a.full, no_add=a.no_add, track=a.track, allow_protected=a.allow_protected, dry_run=a.dry_run)
    return 0


def usage_log_path(cfg):
    return config.expand((cfg.get('usage') or {}).get('log') or os.path.join(config.state_dir(cfg), 'usage.jsonl'))


def read_now(cfg, path, now):
    """`tutti usage read`: every automatic reader once and the recent job logs; prints what was found and any problem."""
    r = readers.Readers(cfg)
    found = r.read_all(now, force=True)
    written = readers.record(path, found, r.scan_jobs(now), now)
    for name, x in found.items():
        if x['problem']:
            say(f'{name}: {x["problem"]}')
    say(f'usage read: {len(found)} account(s) with a reader, {len(written)} new reading(s) in {path}')


def cmd_usage(cfg, a):
    path = usage_log_path(cfg)
    now = pacing.parse_time(a.at) if a.at else datetime.now(timezone.utc)
    if a.action in ('read', 'refresh'):
        read_now(cfg, path, now)
    for problem in readers.record_files(path, cfg['accounts'])[1]:
        say(problem)
    if a.action == 'log':
        if a.account in cfg['disabled_accounts'] and a.percent is not None:
            pacing.append_log(path, a.account, float(a.percent), now, a.note, resets=a.resets)
            say(f'{a.account} is turned off (enabled: false in {cfg["_path"]}): the reading is kept in the log but not used')
            return 0
        if a.account not in cfg['accounts'] or a.percent is None:
            names = ', '.join(cfg['accounts']) or '(none)'
            say(f'usage: tutti usage log <account> <percent> [--resets <ISO time>]; the accounts are {names}')
            return 2
        pacing.append_log(path, a.account, float(a.percent), now, a.note, resets=a.resets)
    entries = pacing.read_log(path)
    print(pacing.report(cfg['accounts'], entries, now))
    for name, acc in cfg['accounts'].items():
        s = pacing.account_state(name, acc, entries, now)
        if s.get('remind'):
            say(f'reminder: {s["remind"]}')
    return 0


def cmd_workers(cfg, a):
    print(workers.listing(cfg))
    return 0


def choose_worker(cfg, now):
    """`tutti run auto`: the worker whose account has room, from the latest readings (panic and spent accounts are skipped)."""
    entries = pacing.read_log(usage_log_path(cfg))
    accounts = [pacing.account_state(n, x, entries, now) for n, x in cfg['accounts'].items()]
    return state.next_worker(cfg, accounts)


def cmd_run(cfg, a):
    extra = []
    if a.worker == 'auto':
        pick = choose_worker(cfg, datetime.now(timezone.utc))
        if not pick['worker']:
            say(f'run auto: {pick["reason"]}')
            return 3
        say(f'run auto: {pick["worker"]} {" ".join(pick["args"])} ({pick["reason"]})'.replace('  ', ' '))
        a.worker, extra, a.account = pick['worker'], pick['args'], pick['account']
    worker_of(cfg, a.worker, 'cli')
    if a.input and not a.on:
        say('run --input: name the machine the inputs go to with --on <machine> (the worker then finds them there)')
        return 2
    if a.show:
        print(workers.brief(cfg, a.task, a.files))
        return 0
    if a.repo:
        return run_delegated(cfg, a, extra)
    with job_inputs(cfg, machine_of(cfg, a.on) if a.input else None, a.input) as base:
        if base:
            names = ', '.join(os.path.basename(os.path.abspath(p).rstrip(os.sep)) for p in a.input)
            a.task += f'\n\nInput files ({names}) are on the machine {a.on} in {base}.'
        return run_worker(cfg, a, extra)


def run_worker(cfg, a, extra):
    rc, lines = workers.run(cfg, a.worker, a.task, a.files, cwd=a.dir, extra=extra)
    note_limits(cfg, a.worker, lines)
    delegate.log_run(cfg, a.worker, getattr(a, 'account', None) or state.account_of_worker(cfg, a.worker), rc, None, lines)
    return rc


def run_in_background(cfg, a, extra):
    """`tutti run ... --bg`: the same run, detached, its output in the run's log; prints the id and the wait line at once."""
    run_id = delegate.new_id(cfg)
    p = delegate.paths(cfg, run_id)
    argv = [sys.executable, remote.BIN, '--config', cfg['_path'], 'run', a.worker, a.task, *a.files, '--repo', a.repo, '--id', run_id]
    argv += [f'--extra={x}' for x in extra]
    argv += [flag for flag, on in (('--no-checks', a.no_checks), ('--discard', a.discard)) if on]
    argv += ['--track', a.track] if a.track else []
    os.makedirs(os.path.dirname(p['log']), exist_ok=True)
    with open(p['log'], 'a', encoding='utf8') as log:
        child = subprocess.Popen(argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    with open(p['pid'], 'w', encoding='utf8') as f:
        f.write(f'{child.pid}\n')
    print(f'run {run_id} started in the background (pid {child.pid}); wait for it with: tutti wait run {run_id}')
    return 0


def run_delegated(cfg, a, extra):
    """`tutti run <worker> "<task>" --repo <repo>`: worktree, headless worker, diff, targeted checks, report."""
    extra = [*extra, *(a.extra or [])]
    if a.bg:
        return run_in_background(cfg, a, extra)
    r = delegate.delegate(cfg, a.worker, a.task, a.files, a.repo, run_id=a.id, track=a.track, extra=extra,
                          checks=not a.no_checks, discard=a.discard)
    with open(delegate.paths(cfg, r['id'])['rc'], 'w', encoding='utf8') as f:
        f.write(f'{r["rc"]}\n')
    with open(delegate.paths(cfg, r['id'])['log'], encoding='utf8', errors='replace') as f:
        lines = f.read().splitlines()[-workers.KEEP_LINES:]
    note_limits(cfg, a.worker, lines)
    delegate.log_run(cfg, a.worker, getattr(a, 'account', None) or state.account_of_worker(cfg, a.worker), r['rc'], r['id'], lines)
    with open(r['report'], encoding='utf8') as f:
        print(f.read().rstrip())
    return r['rc']


def note_limits(cfg, worker, lines):
    """A usage-limit message in the worker's output logs its account as spent."""
    now = datetime.now(timezone.utc)
    cmd_text = ' '.join(cfg['workers'][worker]['command'])
    hits = readers.scan_text(cfg['accounts'], cmd_text, lines, now, f'run-{worker}-{int(now.timestamp())}')
    if hits:
        readers.record(usage_log_path(cfg), {}, hits, now)
        for name, reset, _ref, _text in hits[:1]:
            say(f'{worker} hit a usage limit: {name} is logged as spent'
                + (f' until {reset.isoformat()}' if reset else ' (no reset time in its message)') + '; send new work to other workers')


def cmd_pr(cfg, a):
    """`tutti pr review <n>`: reviewers, the wait, the fallback review."""
    return review.review(cfg, a.number, a.repo, a.timeout, post=not a.no_post, say=say)


def cmd_job(cfg, a):
    """`tutti job register|done`: a script outside tutti shows on the dashboard with its log, progress and result."""
    folder = os.path.join(config.state_dir(cfg), 'jobs')
    pid = a.pid or os.getppid()
    if a.action == 'register':
        path = discover.register(folder, pid, a.title or f'job {pid}', a.log)
        say(f'job {pid} registered ({path})')
        return 0
    if a.rc is None:
        say('job done needs the result: tutti job done --pid <pid> --rc <exit code>')
        return 2
    if not discover.finish(folder, pid, a.rc):
        say(f'no job {pid} in {folder}: register it first (tutti job register --pid {pid} --title ...)')
        return 1
    return 0


def cmd_runs(cfg, a):
    """`tutti runs`: the delegated runs; `show <id>` prints a report, `discard <id>` removes its worktree."""
    if a.action == 'list':
        print(delegate.listing(cfg))
        return 0
    if not a.id or not delegate.valid_id(a.id):
        say(f'name the run by its id: tutti runs {a.action} <id> (tutti runs lists them)')
        return 2
    r = delegate.load_report(cfg, a.id)
    if not r:
        say(f'no report for run {a.id} in {delegate.runs_dir(cfg)}: it may still be running (tutti wait run {a.id})')
        return 1
    if a.action == 'show':
        with open(delegate.paths(cfg, a.id)['report'], encoding='utf8') as f:
            print(f.read().rstrip())
        return 0
    wt = delegate.own_worktree(cfg, r.get('worktree'))
    if not wt:
        say(f'run {a.id}: its report names a worktree outside {cfg["project"]["worktrees"]}: nothing removed')
        return 1
    if os.path.isdir(wt):
        delegate.remove_tree(wt, say)
    else:
        say(f'run {a.id}: its worktree {wt} is already gone')
    return 0


def cmd_ask(cfg, a):
    stdin_text = '' if sys.stdin.isatty() else sys.stdin.read()
    print(local.ask(cfg, worker_of(cfg, a.worker, 'ollama'), a.instruction, a.files, stdin_text))
    return 0


def cmd_code(cfg, a):
    print(local.code(cfg, worker_of(cfg, a.worker, 'ollama'), a.file, a.instruction, a.context))
    return 0


def cmd_agent(cfg, a):
    ok = local.agent(cfg, worker_of(cfg, a.worker, 'ollama'), a.task, a.check, a.files, a.rounds, a.dir)
    return 0 if ok else 1


def cmd_yield(cfg, a):
    names = [a.machine] if a.machine else list(cfg['machines'])
    if a.action != 'status':
        if not a.machine:
            say('name the machine: tutti yield on --machine <name> (a flag is raised on one machine, never on all by default)')
            return 2
        note = ('unload ' if a.unload else '') + a.note
        if remote.set_yield(machine_of(cfg, a.machine), a.action == 'on', note) != 0:
            say(f'could not change the yield flag on {a.machine}: check that the machine is reachable (tutti machines)')
            return 1
    for name in names:
        note = remote.yield_note(machine_of(cfg, name))
        print(f'{name:12} {"free" if note is None else "YIELD " + note}'.rstrip())
    return 0


def progress_target(cfg, job):
    if not job:
        return os.environ.get('TUTTI_PROGRESS_FILE')
    name = job.split(':', 1)[-1]   # the dashboard's id (quiet:<log>) or the log's name or path
    log = name if os.sep in name else os.path.join(config.state_dir(cfg), 'quiet', name)
    return log + '.progress'


def cmd_progress(cfg, a):
    """`tutti progress <n>/<m>`: how far the job is, for the dashboard's progress bar."""
    try:
        done, total = (int(x) for x in a.value.split('/'))
    except ValueError:
        done, total = -1, 0
    if total <= 0 or not 0 <= done <= total:
        say(f'progress {a.value!r}: write <done>/<total> with whole numbers, done at most total (for example 150/177)')
        return 2
    path = progress_target(cfg, a.job)
    if not path:
        say('progress: no job to report for: run it inside `tutti quiet` (it sets TUTTI_PROGRESS_FILE) or pass --job <log>')
        return 2
    with open(path, 'w', encoding='utf8') as f:
        f.write(f'{done} {total}\n')
    return 0


def cmd_quiet(cfg, a):
    env = dict(os.environ, QUIET_DIR=os.path.join(config.state_dir(cfg), 'quiet'))
    if a.summary:
        worker_of(cfg, a.summary, 'ollama')
        me = f'{shlex.quote(sys.executable)} {shlex.quote(remote.BIN)} --config {shlex.quote(cfg["_path"])}'
        env['QUIET_SUMMARY'] = f'{me} ask {shlex.quote(a.summary)} {shlex.quote(SUMMARY)}'
    return subprocess.run([os.path.join(SCRIPTS, 'quiet.sh'), *a.command], env=env).returncode


def cmd_guard(cfg, a):
    g = cfg.get('guard') or {}
    image = g.get('image') or cfg['runner'].get('image')
    if not image:
        say('guard needs an image to watch: set guard.image (or runner.image) in the config')
        return 2
    env = dict(os.environ, GUARD_IMAGE=image, LOW_GB=str(g.get('low_gb', 3)), HIGH_GB=str(g.get('high_gb', 6)))
    return subprocess.run([os.path.join(SCRIPTS, 'memguard.sh')], env=env).returncode


def cmd_tunnel(cfg, a):
    w = worker_of(cfg, a.worker, 'ollama')
    t = w.get('tunnel')
    if not t:
        say(f'workers.{a.worker} has no tunnel: add tunnel: {{ssh: <host>, remote: 127.0.0.1:11434}} to reach its model server')
        return 2
    env = dict(os.environ, TUTTI_STATE=config.state_dir(cfg))
    return subprocess.run([os.path.join(SCRIPTS, 'tunnel.sh'), a.action, a.worker, w['host'], t['ssh'], t['remote']], env=env).returncode


def wait_run_args(cfg, a):
    """`tutti wait run <id>`: a delegated run is done when its .rc file appears; its log is what stalls. A run whose process
    is gone without an .rc file ended badly and is reported at once instead of waited on until the timeout."""
    if not delegate.valid_id(a.target):
        raise config.ConfigError(f'"{a.target}" is not a run id (<time>-<pid>): `tutti runs` lists the runs')
    p = delegate.paths(cfg, a.target)
    if not os.path.exists(p['log']):
        raise config.ConfigError(f'no run {a.target} in {delegate.runs_dir(cfg)}: `tutti runs` lists the runs')
    a.stall_file = a.stall_file or p['log']
    a.stall_watch = [a.stall_file, p['dir']]   # the run's log and its folder, where each check writes its own log
    a.stall = float(cfg['dashboard']['stall_minutes']) * 60 if a.stall is None else a.stall   # --stall 0: off
    pid = state.read_rc(p['pid'])
    gone = waiting.pid_gone(pid) if pid else (lambda: False)
    return lambda: os.path.exists(p['rc']) or gone(), p['rc']


def finished_line(cfg, a, started):
    """The one notification line when a wait ends well: what, after how long, and for a run its verdict and report."""
    took = f'{time.monotonic() - started:.0f} s'
    if a.kind != 'run':
        return f'done: {a.kind} {a.target}{" " + a.pattern if a.pattern else ""} after {took}', 0
    p = delegate.paths(cfg, a.target)
    rc = state.read_rc(p['rc'])
    r = delegate.load_report(cfg, a.target)
    if rc is None or not r:
        return f'run {a.target} ended after {took} without a report: its process is gone; read {p["log"]}', 1
    checks = r['checks'] or []
    failed = sum(1 for c in checks if c['rc'] != 0)
    nocov = ' (coverage not checked)' if any(c['coverage'] for c in checks) else ''
    tested = 'not tested' if r['checks'] is None or not checks else f'checks {len(checks) - failed}/{len(checks)} passed{nocov}'
    return (f'run {a.target} done after {took}: {r["worker"]} rc {r["worker_rc"]}, {len(r["changed"])} file(s), {tested}'
            f'{", DENIED tool calls" if r["denied"] else ""}; report {r["report"]}'), rc


def wait_check(cfg, a):
    """(the done check, the target as recorded) for one kind of wait."""
    if a.kind == 'run':
        return wait_run_args(cfg, a)
    checks = {'pid': lambda: waiting.pid_gone(a.target), 'file': lambda: waiting.file_exists(a.target),
              'grep': lambda: waiting.file_matches(a.target, a.pattern or '')}
    # Absolute paths: the dashboard reads the record from its own working directory.
    return checks[a.kind](), a.target if a.kind == 'pid' else os.path.abspath(a.target)


def wait_ended(cfg, a, ended, seen, started):
    """The exit code and the one line a wait ends with."""
    if ended and seen:
        line, rc = finished_line(cfg, a, started)
        print(line)
        return rc
    if ended:
        say(f'wait {a.kind} {a.target}: {a.stall_file or a.target} has not changed for {a.stall:g} s; the job looks stalled: '
            'read its log, then stop or restart it (do not wait again on the same job)')
        return 125
    say(f'wait {a.kind} {a.target}: not seen within {a.timeout:g} s; the thing waited for may have failed: look at its log')
    return 124


def cmd_wait(cfg, a):
    if a.kind == 'grep' and not a.pattern:
        say('wait grep needs a pattern: tutti wait grep <file> <regex>')
        return 2
    started = time.monotonic()
    done, target = wait_check(cfg, a)
    watched = getattr(a, 'stall_watch', None) or a.stall_file or a.target
    stall = waiting.stalled(watched, a.stall) if a.stall else (lambda: False)
    seen = []
    watch = os.path.abspath(a.stall_file) if a.stall_file else target
    record = waiting.register(os.path.join(config.state_dir(cfg), 'waits'), {
        'kind': a.kind, 'target': target, 'pattern': a.pattern or '', 'watch': watch, 'timeout': a.timeout, 'stall': a.stall})

    def check():
        if done():
            seen.append(True)
            return True
        return stall()
    try:
        ended = waiting.until(check, a.timeout, a.poll)
    finally:
        waiting.unregister(record)
    return wait_ended(cfg, a, ended, seen, started)


def cmd_translate(cfg, a):
    if a.check:
        problem = translate.check(a.source)
        print(problem or f'{a.source}: QA report matches')
        return 1 if problem else 0
    t = cfg.get('translate') or {}
    for role in ('translator', 'judge'):
        worker_of(cfg, t.get(role, ''), 'ollama')
    if not a.locale:
        say('name the locale: tutti translate <source.json> <locale>')
        return 2
    passed, held = translate.run(cfg, a.source, a.locale, a.out)
    print(f'{a.locale}: {passed} passed QA, {held} held for a human ({os.path.join(a.out, "held-" + a.locale + ".jsonl")})')
    return 0


def targets(cfg, name):
    """The machines a files command acts on: one by name, or `all` (every machine that is not this computer)."""
    if name == 'all':
        out = [m for m in cfg['machines'].values() if m['transport'] != 'local']
        if not out:
            raise config.ConfigError(f'--to all: {cfg["_path"]} has no machine other than this computer')
        return out
    return [machine_of(cfg, name)]


def cmd_files(cfg, a):
    try:
        return a.files_fn(cfg, a)
    except OSError as e:
        say(f'files {a.action}: {e}: check the path and the disk, then run it again')
        return 1


def files_send(cfg, a):
    failed = []
    for m in targets(cfg, a.to):
        try:
            base, result = transfer.send(cfg, m, [os.path.abspath(p) for p in a.paths], dest=a.dest, bwlimit=a.bwlimit)
        except RuntimeError as e:
            say(f'{m["name"]}: {e}')
            failed.append(m['name'])
            continue
        for name, how, size in result:
            print(f'{m["name"]}: {name} {how} ({files.human(size)}) in {base}')
    if failed:
        say(f'files send: failed on {", ".join(failed)}; what reached the other machines is stored. Fix the cause above and send '
            'again: finished files are skipped and partial ones resume')
    return 1 if failed else 0


def files_list(cfg, a):
    for m in targets(cfg, a.on or 'all'):
        st = transfer.stored(cfg, m)
        if st['problem']:
            print(f'{m["name"]}: {st["problem"]}')
            continue
        print(f'{m["name"]}: {files.human(st["bytes"])} of {files.human(st["budget_bytes"])} in {st["cache"]}')
        for it in st['items']:
            used = datetime.fromtimestamp(it['used_at'], timezone.utc).strftime('%Y-%m-%d %H:%M')
            print(f'  {it["name"]:40} {files.human(it["bytes"]):>10}  used {used} UTC')
    return 0


def files_clean(cfg, a):
    for m in targets(cfg, a.on):
        gone = transfer.clean(cfg, m, a.names, everything=a.all)
        print(f'{m["name"]}: removed {", ".join(gone) if gone else "nothing (the cache is within its budget)"}')
    return 0


def cmd_dashboard(cfg, a):
    collector = dashboard.Collector(cfg)
    if a.once:
        if not a.no_probe:
            collector.refresh()
        print(json.dumps(collector.snapshot(), indent=2))
        return 0
    port = cfg['dashboard']['port'] if a.port is None else a.port
    try:
        httpd = dashboard.server(cfg, port, collector)
    except OSError as e:
        say(f'cannot listen on {dashboard.BIND}:{port} ({e.strerror}): another dashboard may be running there; open it, '
            'or pass --port <n> (or set dashboard.port)')
        return 1
    if not a.no_probe:
        collector.start()
    say(f'dashboard: http://{dashboard.BIND}:{httpd.server_address[1]}/ (this computer only; Ctrl+C stops it)')
    try:
        httpd.serve_forever()
    finally:
        collector.stop.set()
        httpd.server_close()
    return 0


def cmd_conduct(cfg, a):
    """`tutti conduct --main <agent> "<goal>"`: the chosen agent conducts with Tutti's commands; others take over on panic."""
    fallback = [x for x in a.fallback.split(',') if x] if a.fallback is not None else None
    commands = set(SUBCOMMANDS)
    if a.show:
        now = datetime.now(timezone.utc)
        agent = a.main or conduct.current(cfg)
        print(conduct.instructions(cfg, agent, a.goal, os.path.abspath(a.dir), '<run>/progress.md', commands, now))
        return 0
    try:
        rc, _run = conduct.conduct(cfg, a.goal, main=a.main, fallback=fallback, cwd=a.dir, commands=commands, say=say)
    except conduct.ConductError as e:
        say(str(e))
        return 2
    return rc


def cmd_main(cfg, a):
    """`tutti main`: the main agent and who has room; `tutti main <agent>` switches it (a running conductor hands over)."""
    if a.agent:
        try:
            conduct.choose(cfg, a.agent)
        except conduct.ConductError as e:
            say(str(e))
            return 2
    print(conduct.describe(cfg))
    return 0


def cmd_handoff(cfg, a):
    try:
        path = conduct.note_progress(os.environ.get('TUTTI_CONDUCT'), a.section, a.text)
    except conduct.ConductError as e:
        say(str(e))
        return 2
    say(f'handoff: noted under {a.section} in {path}')
    return 0


SUBCOMMANDS = []


def parser():
    p = argparse.ArgumentParser(prog='tutti', description='One orchestrator, many workers, shared machines.')
    p.add_argument('--version', action='version', version=f'tutti {__version__}')
    p.add_argument('--config', help='path of tutti.config.yaml (default: $TUTTI_CONFIG, then the nearest one upwards)')
    sub = p.add_subparsers(dest='cmd', required=True)

    def add(name, fn, text):
        if name not in SUBCOMMANDS:
            SUBCOMMANDS.append(name)
        s = sub.add_parser(name, help=text, description=text)
        s.set_defaults(fn=fn)
        return s

    add('config', cmd_config, 'check the config and print its path')
    add('machines', cmd_machines, 'probe every machine: free slots, load, yield flag')
    s = add('test', cmd_test, 'run a command over a track\'s work tree on the first fit machine, behind its slot queue')
    s.add_argument('--dir', help='the work tree (default: <project.worktrees>/<track>)')
    s.add_argument('--on', help='skip routing and use this machine')
    s.add_argument('--input', action='append', metavar='PATH',
                   help='a file or folder the job needs: copied to the machine\'s cache first, its folder in $TUTTI_INPUTS (repeat it)')
    s.add_argument('track')
    # REMAINDER: `tutti test t echo -n x` keeps -n for the command instead of reading it as an option of tutti (FEEDBACK #4)
    s.add_argument('command', nargs=argparse.REMAINDER)
    s = add('rsh', cmd_rsh, 'remote-shell shim for git and rsync on a WSL machine (internal)')
    s.add_argument('machine')
    s.add_argument('args', nargs=argparse.REMAINDER)
    s = add('ship', cmd_ship, 'format, commit, check, push and deploy the work tree you are in')
    s.add_argument('message')
    s.add_argument('--full', action='store_true', help='run ship.full_checks instead of the targeted ship.checks')
    s.add_argument('--no-add', action='store_true', help='commit only what is already staged')
    s.add_argument('--track', help='refuse unless this directory is that track\'s worktree')
    s.add_argument('--allow-protected', action='store_true')
    s.add_argument('--dry-run', action='store_true', help='print the plan; change nothing')
    s.add_argument('--dir', default='.')
    s = add('usage', cmd_usage, 'show every account against its pace; `usage log <account> <percent>` records a reading, '
                                '`usage refresh` (or read) runs the automatic readers now')
    s.add_argument('action', nargs='?', choices=['log', 'read', 'refresh'])
    s.add_argument('account', nargs='?')
    s.add_argument('percent', nargs='?')
    s.add_argument('--at', help='the time of the reading or of the report (ISO 8601, UTC by default)')
    s.add_argument('--note', default='')
    s.add_argument('--resets', help='usage log: the reset time the usage page shows (ISO 8601); it anchors the window from then on')
    add('workers', cmd_workers, 'list the workers and whether each is ready')
    s = add('run', cmd_run, 'brief a CLI worker (cursor, agy, claude; `auto`: the one whose account has room) in a worktree; '
                            'it must not commit')
    s.add_argument('worker')
    s.add_argument('task')
    s.add_argument('files', nargs='*')
    s.add_argument('--dir', help='the worktree the worker edits (default: here)')
    s.add_argument('--show', action='store_true', help='print the brief and stop')
    s.add_argument('--input', action='append', metavar='PATH', help='a file or folder to copy to the --on machine first (repeat it)')
    s.add_argument('--on', help='the machine --input files go to')
    s.add_argument('--repo', help='hand the task off end to end: a worktree of this repo, the worker headless, the diff, the checks, '
                                  'a report (tutti runs show <id>)')
    s.add_argument('--track', help='with --repo: reuse or create the worktree <delegate.prefix><track> instead of a new one')
    s.add_argument('--bg', action='store_true', help='with --repo: run detached; prints the id for `tutti wait run <id>`')
    s.add_argument('--no-checks', action='store_true', help='with --repo: skip the checks (the report says nothing was tested)')
    s.add_argument('--discard', action='store_true', help='with --repo: remove a new worktree after the report (the diff file stays)')
    s.add_argument('--id', help=argparse.SUPPRESS)
    s.add_argument('--extra', action='append', help=argparse.SUPPRESS)
    s = add('pr', cmd_pr, 'pull requests: `pr review <n>` asks the reviewers, says when none will come, and falls back to a local '
                          'review by a cheap worker posted to the pull request')
    s.add_argument('action', choices=['review'])
    s.add_argument('number', type=int)
    s.add_argument('--repo', help='owner/name (default: the repository of the current directory)')
    s.add_argument('--timeout', type=float, help='minutes to wait for the reviewers (default: review.timeout_minutes)')
    s.add_argument('--no-post', action='store_true', help='print the fallback review instead of posting it')
    s = add('job', cmd_job, 'register a script\'s run for the dashboard (`job register --title t`), and its result (`job done --rc n`)')
    s.add_argument('action', choices=['register', 'done'])
    s.add_argument('--pid', type=int, help='the process (default: the caller, the shell that runs tutti)')
    s.add_argument('--title', help='what the job does')
    s.add_argument('--log', help='its log (default: where the process\'s standard output goes, when that is a file)')
    s.add_argument('--rc', type=int, help='job done: the exit code')
    s = add('runs', cmd_runs, 'the delegated runs: list, show <id> (the report), discard <id> (removes its worktree)')
    s.add_argument('action', nargs='?', default='list', choices=['list', 'show', 'discard'])
    s.add_argument('id', nargs='?')
    s = add('ask', cmd_ask, 'a short answer from a local model about files or stdin')
    s.add_argument('worker')
    s.add_argument('instruction')
    s.add_argument('files', nargs='*')
    s = add('code', cmd_code, 'a local model rewrites one file in place; review with git diff')
    s.add_argument('worker')
    s.add_argument('file')
    s.add_argument('instruction')
    s.add_argument('context', nargs='*')
    s = add('agent', cmd_agent, 'a local model edits, runs the check, reads the failure and retries')
    s.add_argument('worker')
    s.add_argument('task')
    s.add_argument('check')
    s.add_argument('files', nargs='+')
    s.add_argument('--rounds', type=int, default=4)
    s.add_argument('--dir', default='.')
    s = add('yield', cmd_yield, 'raise or lower a machine\'s yield flag: while it is up, tutti sends that machine nothing new')
    s.add_argument('action', choices=['on', 'off', 'status'])
    s.add_argument('--machine')
    s.add_argument('--note', default='')
    s.add_argument('--unload', action='store_true', help='also ask local-model workers to unload their models')
    s = add('quiet', cmd_quiet, 'run a command, keep its log, print only the exit code (and a short failure summary)')
    s.add_argument('--summary', help='the local-model worker that summarises a failure')
    s.add_argument('command', nargs='+')
    s = add('progress', cmd_progress, 'report how far a job is (<done>/<total>) for the dashboard\'s progress bar')
    s.add_argument('value')
    s.add_argument('--job', help='the job: its log, or the dashboard id quiet:<log> (default: $TUTTI_PROGRESS_FILE, set by tutti quiet)')
    add('guard', cmd_guard, 'pause test containers when memory runs low, resume them when it is back')
    s = add('tunnel', cmd_tunnel, 'the self-healing ssh tunnel to a worker\'s model server')
    s.add_argument('action', choices=['start', 'stop', 'status'])
    s.add_argument('worker')
    s = add('wait', cmd_wait, 'wait for a pid to end, a file to appear or a line in a log; always with a timeout')
    s.add_argument('kind', choices=['pid', 'file', 'grep', 'run'], help='run: a `tutti run --repo --bg` id; ends with its verdict')
    s.add_argument('target')
    s.add_argument('pattern', nargs='?')
    s.add_argument('--timeout', type=float, default=3600)
    s.add_argument('--poll', type=float, default=2)
    s.add_argument('--stall', type=float, default=None, help='give up (exit 125) when the log has not changed for this many seconds')
    s.add_argument('--stall-file', help='the log watched by --stall (default: the target)')
    s = add('translate', cmd_translate, 'translate a flat JSON catalogue with mandatory QA; --check verifies an output\'s report')
    s.add_argument('source')
    s.add_argument('locale', nargs='?')
    s.add_argument('--out', default='i18n-out')
    s.add_argument('--check', action='store_true')
    s = add('files', cmd_files, 'copy files to machines (`send`), see what each stores (`list`), trim or empty its cache (`clean`)')
    fsub = s.add_subparsers(dest='action', required=True)
    f = fsub.add_parser('send', help='copy files and folders to a machine, or all')
    f.set_defaults(files_fn=files_send)
    f.add_argument('paths', nargs='+', help='files and folders on this computer')
    f.add_argument('--to', required=True, help='a machine, or all')
    f.add_argument('--dest', help='a folder on the machine instead of its cache (not counted against the budget)')
    f.add_argument('--bwlimit', type=int, help='at most this many KB/s (default files.bwlimit_kbps; 0 = no limit)')
    f = fsub.add_parser('list', help='what each machine stores')
    f.set_defaults(files_fn=files_list)
    f.add_argument('--on', help='a machine, or all (the default)')
    f = fsub.add_parser('clean', help='trim a cache to its budget, remove named items, or empty it')
    f.set_defaults(files_fn=files_clean)
    f.add_argument('names', nargs='*', help='items to remove (default: trim to the budget)')
    f.add_argument('--on', required=True, help='a machine, or all')
    f.add_argument('--all', action='store_true', help='remove every item and partial file in the cache')
    s = add('dashboard', cmd_dashboard, 'a local web dashboard (installable app): usage and pacing, machines, workers and jobs')
    s.add_argument('--port', type=int, help='the port on 127.0.0.1 (default: dashboard.port, 4777)')
    s.add_argument('--once', action='store_true', help='print the state as JSON (what /api/state answers) and stop')
    s.add_argument('--no-probe', action='store_true', help='do not probe machines and workers (ssh, model servers)')
    s = add('conduct', cmd_conduct, 'another agent (cursor, agy, copilot, a local model) as the main agent: it gets the goal, an '
                                    'instruction file with Tutti\'s commands and the safety limits; on panic the next one takes over')
    s.add_argument('goal')
    s.add_argument('--main', help='the main agent, a worker (default: the dashboard\'s choice, else main.agent)')
    s.add_argument('--fallback', help='comma-separated workers that take over in turn (default: main.fallback)')
    s.add_argument('--dir', default='.', help='the project folder the main agent works in (default: here)')
    s.add_argument('--show', action='store_true', help='print the instruction file and stop')
    s = add('handoff', cmd_handoff, 'inside `tutti conduct`: record a finished step (done) or the next one (next) for the handoff note')
    s.add_argument('section', choices=['done', 'next'])
    s.add_argument('text')
    s = add('main', cmd_main, 'the main agent: show it and who has room, or `tutti main <agent>` to switch it')
    s.add_argument('agent', nargs='?')
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        cfg = config.load(a.config)
        os.environ['TUTTI_CONFIG'] = cfg['_path']
        return a.fn(cfg, a)
    except (config.ConfigError, ship.ShipError, RuntimeError, ValueError) as e:
        say(str(e))
        return 1
    except KeyboardInterrupt:
        return 130
