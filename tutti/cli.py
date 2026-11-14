"""The `tutti` command. Every subcommand reads tutti.config.yaml; nothing personal is built in."""
import argparse
import json
import os
import shlex
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

from . import (
    __version__,
    config,
    dashboard,
    files,
    local,
    pacing,
    readers,
    remote,
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
        print(f'{name:12} {m["transport"]:5} slots {p.free}/{m["slots"]}  load {p.load:g}/{p.cores}  {state}')
    return 0


def cmd_test(cfg, a):
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
    if a.action == 'log':
        if a.account not in cfg['accounts'] or a.percent is None:
            names = ', '.join(cfg['accounts']) or '(none)'
            say(f'usage: tutti usage log <account> <percent> [--resets <ISO time>]; the accounts are {names}')
            return 2
        pacing.append_log(path, a.account, float(a.percent), now, a.note, resets=a.resets)
    print(pacing.report(cfg['accounts'], pacing.read_log(path), now))
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
        a.worker, extra = pick['worker'], pick['args']
    worker_of(cfg, a.worker, 'cli')
    if a.input and not a.on:
        say('run --input: name the machine the inputs go to with --on <machine> (the worker then finds them there)')
        return 2
    if a.show:
        print(workers.brief(cfg, a.task, a.files))
        return 0
    with job_inputs(cfg, machine_of(cfg, a.on) if a.input else None, a.input) as base:
        if base:
            names = ', '.join(os.path.basename(os.path.abspath(p).rstrip(os.sep)) for p in a.input)
            a.task += f'\n\nInput files ({names}) are on the machine {a.on} in {base}.'
        return run_worker(cfg, a, extra)


def run_worker(cfg, a, extra):
    rc, lines = workers.run(cfg, a.worker, a.task, a.files, cwd=a.dir, extra=extra)
    now = datetime.now(timezone.utc)
    cmd_text = ' '.join(cfg['workers'][a.worker]['command'])
    hits = readers.scan_text(cfg['accounts'], cmd_text, lines, now, f'run-{a.worker}-{int(now.timestamp())}')
    if hits:
        readers.record(usage_log_path(cfg), {}, hits, now)
        for name, reset, _ref, _text in hits[:1]:
            say(f'{a.worker} hit a usage limit: {name} is logged as spent'
                + (f' until {reset.isoformat()}' if reset else ' (no reset time in its message)') + '; send new work to other workers')
    return rc


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


def cmd_wait(cfg, a):
    checks = {'pid': lambda: waiting.pid_gone(a.target), 'file': lambda: waiting.file_exists(a.target),
              'grep': lambda: waiting.file_matches(a.target, a.pattern or '')}
    if a.kind == 'grep' and not a.pattern:
        say('wait grep needs a pattern: tutti wait grep <file> <regex>')
        return 2
    done = checks[a.kind]()
    stall = waiting.stalled(a.stall_file or a.target, a.stall) if a.stall else (lambda: False)
    seen = []
    # Absolute paths: the dashboard reads the record from its own working directory.
    target = a.target if a.kind == 'pid' else os.path.abspath(a.target)
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
    if ended:
        if seen:
            return 0
        say(f'wait {a.kind} {a.target}: {a.stall_file or a.target} has not changed for {a.stall:g} s; the job looks stalled: '
            'read its log, then stop or restart it (do not wait again on the same job)')
        return 125
    say(f'wait {a.kind} {a.target}: not seen within {a.timeout:g} s; the thing waited for may have failed: look at its log')
    return 124


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


def parser():
    p = argparse.ArgumentParser(prog='tutti', description='One orchestrator, many workers, shared machines.')
    p.add_argument('--version', action='version', version=f'tutti {__version__}')
    p.add_argument('--config', help='path of tutti.config.yaml (default: $TUTTI_CONFIG, then the nearest one upwards)')
    sub = p.add_subparsers(dest='cmd', required=True)

    def add(name, fn, text):
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
    s.add_argument('command', nargs='+')
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
    s.add_argument('kind', choices=['pid', 'file', 'grep'])
    s.add_argument('target')
    s.add_argument('pattern', nargs='?')
    s.add_argument('--timeout', type=float, default=3600)
    s.add_argument('--poll', type=float, default=2)
    s.add_argument('--stall', type=float, default=0, help='give up (exit 125) when the log has not changed for this many seconds')
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
