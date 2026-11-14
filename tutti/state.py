"""The dashboard's state: usage and pacing, machines, workers and jobs, assembled as plain JSON-ready data.
Pure assembly over what Tutti already keeps (the config, the usage log, the state folder); the probes are passed in."""
import glob
import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import __version__, config, controls, devices, pacing, routing, waiting, workers

# CLI agents worth knowing about even when the config does not name them yet: the command and how people call it.
KNOWN_AGENTS = (('claude', 'Claude Code'), ('cursor-agent', 'Cursor'), ('agy', 'Antigravity'), ('copilot', 'GitHub Copilot CLI'),
                ('gemini', 'Gemini CLI'), ('ollama', 'Ollama'))
TAIL_LINES = 40
TAIL_WIDTH = 400
MAX_JOBS = 40
ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
# Progress lines, most specific first; a job's progress is the last line of its log any of them matches. Bare `a/b` and
# bare percentages are not used: dates, ratios and coverage figures would read as progress.
PROGRESS = (
    ('steps', re.compile(r'\[\s*(\d+)\s*/\s*(\d+)\s*\]')),                       # playwright [150/177], many build tools
    ('stage', re.compile(r'\b(?:stage|step|gate|phase|chunk)\s+(\d+)\s*(?:/|of)\s*(\d+)\b', re.I)),
    ('percent', re.compile(r'\[\s*(\d{1,3})\s*%\s*\]')),                            # pytest [ 13%]
    ('tests', re.compile(r'\b(\d+)\s*/\s*(\d+)\s+(?:tests?|files?|specs?|passed|done)\b', re.I)),
    ('gates', re.compile(r'^\s*[a-z][\w-]*\s+(\d+)/(\d+)\s+[\w.:-]+\s*$')),     # a pipeline's gate: "static 3/51 eslint"
    # rsync's progress2 line: bytes, percent, speed
    ('percent', re.compile(r'^ *[\d,]+ +(\d{1,3})% +[\d.]+[kMG]?B/s')),
    ('queue', re.compile(r'slot\.sh: waiting for a free slot \(all (\d+) busy\)')),
)
SLOT_START = re.compile(r'slot\.sh: (?:got|started in) a slot')
LOG_NAME = re.compile(r'^(?:ship-)?(\d+)-(\d+)(?:-\d+)?\.log$')


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def usage_log_path(cfg):
    return config.expand((cfg.get('usage') or {}).get('log') or os.path.join(cfg['project']['state'], 'usage.jsonl'))


def usage_state(cfg, now):
    path = usage_log_path(cfg)
    try:
        entries = pacing.read_log(path)
        problem = None
    except (OSError, ValueError) as e:
        entries, problem = [], f'{path} could not be read ({e}): fix or move the broken line, then `tutti usage` to check'
    accounts = []
    for n, a in cfg['accounts'].items():
        s = with_credits(cfg, pacing.account_state(n, a, entries, now, *pacing.plan_args(cfg)), a or {})
        accounts.append(with_background(cfg, s, a or {}, now))
    accounts = with_scouts(cfg, accounts, now)
    accounts = with_targets(cfg, accounts, now)
    from . import owner
    return {'log': path, 'problem': problem, 'accounts': accounts, 'owner': owner.view(cfg, config.state_dir(cfg), entries, now)}


def target_facts(cfg):
    """What the target line needs from the autopilot: tasks queued, why runs wait, fill_room, the parallel limit."""
    from . import autopilot, backlog, fill
    try:
        tasks = backlog.load(cfg)['tasks']
        st = autopilot.read_state(cfg)
    except (OSError, ValueError, KeyError):
        return {}
    last = st.get('last') or {}
    busy = last.get('reason') if last.get('state') in ('waiting', 'running') and 'queued' not in str(last.get('reason')) else ''
    return {'queued': sum(1 for t in tasks if t.get('status') == 'queued'), 'busy': busy, 'fill_room': fill.fill_on(cfg),
            'parallel': (st.get('parallel') or {}).get('limit')}


def with_targets(cfg, accounts, now):
    """Each account's target line (tutti/target.py): where it is heading by its spend-by time and, when short, why."""
    from . import target
    facts = target_facts(cfg) if cfg.get('autopilot', {}).get('enabled') else {}
    return [{**a, 'outlook': target.outlook(a, now, facts)} for a in accounts]


def with_scouts(cfg, accounts, now):
    """A scout's account (Grok Bot's allowance) takes no bounded tasks: its Do line and Decision say what the scout does with
    the allowance instead (tutti/spendrest.py: the rest is spent on scout work by itself near the reset)."""
    owners = {(sc or {}).get('account'): n for n, sc in (cfg.get('scouts') or {}).items() if isinstance(sc, dict)}
    if not any(a['name'] in owners for a in accounts):
        return accounts
    from . import scouts, spendrest
    requests = scouts.load(cfg)['requests']
    out = []
    for a in accounts:
        name = owners.get(a['name'])
        if not name or a.get('state') != 'ok':
            out.append({**a, 'scout': name} if name else a)
            continue
        p = spendrest.plan(cfg, scouts.settings(cfg, name), requests, a, now.timestamp())
        do = scout_do(name, a, p)
        out.append({**a, 'scout': name, 'room_tasks': None, 'action': do, 'quota_plan': scout_plan(a, p, do)})
    return out


def scout_do(name, a, p):
    """A scout account's Do line: what spend-the-rest does, else what its pace means for the scout (never backlog tasks)."""
    from . import spendrest
    if spendrest.line(p):
        return spendrest.line(p)
    if a['verdict'] in ('behind', 'late'):
        return f'scout {name}: its watches run on their own periods; spend the rest: {p["why"]}'
    if a['verdict'] == 'ahead':
        return f'scout {name}: {a["action"]}'
    return f'scout {name}: its watches run on their own periods'


def scout_plan(a, p, do):
    """The planner rows of a scout account with its Decision and Why replaced: scout work, no tasks."""
    plan = a.get('quota_plan')
    if not plan:
        return plan
    decision = f'scout work: {(p.get("week") or {}).get("now", 0)} run(s) now on its weekly line' if p['on'] else 'scout work only'
    rows = [r for r in plan.get('rows', []) if r['label'] not in ('Decision', 'Why')]
    rows += [{'label': 'Decision', 'value': decision}, {'label': 'Why', 'value': 'a scout answers questions; it takes no backlog tasks'}]
    return {**plan, 'tasks': 0, 'rows': rows, 'why': f'{a["name"]}: {do}'}


def with_background(cfg, s, a, now):
    """For an account with `background: true` (Cursor, whose own automatic reviews spend it outside Tutti): its rise with no
    Tutti run on it, per hour, from this window's readings, and the plan re-made around what that spends until the target."""
    from . import planner, readers
    if not a.get('background') or s.get('state') != 'ok' or a.get('limits'):
        return s
    busy = readers.busy_intervals(os.path.join(config.expand(cfg['project']['state']), 'runs'), a.get('worker') or '', now)
    rate = planner.background_rate(s['readings'], busy)
    s = planner.plan_alone(s, a, now, rate)
    s['background_rate'] = None if rate is None else round(rate, 3)
    return s


def with_credits(cfg, s, a):
    """For an account with `credits: true` (Copilot): the AI credits its runs printed in this window, from Tutti's run and
    quiet logs. Runs started outside Tutti are not counted; the plan's own report (a reader) is the full picture."""
    from . import readers
    if a.get('credits') and s.get('window_start'):
        folder = config.expand(cfg['project']['state'])
        s['credits_used'] = readers.credits_since([os.path.join(folder, 'runs'), os.path.join(folder, 'quiet')],
                                                  pacing.parse_time(s['window_start']))
    return s


# ---- machines


def machine_status(m, p, reason):
    """held (paused by the owner), off (opt-in, not on), offline, paused (its yield flag), busy, free or loaded."""
    if routing.owner_unfit(m):
        return 'held' if m.get('paused') else 'off'
    if not p.reachable:
        return 'offline'
    if p.yielding:
        return 'paused'
    if p.free < 1:
        return 'busy'
    return 'free' if not reason else 'loaded'


def machine_state(m, p, rank, now=None, idle_s=None, quiet_hours=None):
    """One machine as the dashboard shows it, from its config and a probe (routing.Probe). Its free slots are capped at its
    share for Tutti (tutti/share.py: use_pct, lower while someone uses it, in quiet hours or hot)."""
    from . import share
    reason = routing.unfit(m, p)
    status = machine_status(m, p, reason)
    slots = int(m['slots'])
    pct, pct_why = share.pct(m, {'reason': reason}, now, idle_s, quiet_hours)
    free = share.cap_free(slots, min(p.free, slots), pct) if p.reachable else None
    if p.reachable and status == 'free' and not free:
        status, reason = 'busy', f'its share for Tutti is {pct} %' + (f' ({pct_why})' if pct_why else '') + ': no slot left at it'
    return {'name': m['name'], 'transport': m['transport'], 'host': m.get('host', 'local'), 'slots': slots, 'free': free,
            'busy': None if free is None else slots - min(p.free, slots), 'load': p.load if p.reachable else None,
            'cores': p.cores if p.reachable else None,
            'reachable': p.reachable, 'paused': p.yielding, 'status': status, 'reason': reason or 'fit: takes the next run', 'rank': rank,
            'min_free_gb': m['min_free_gb'], 'temp_c': p.temp if p.reachable else None, 'heat': m.get('heat'),
            'gpu_temp_c': p.gpu_temp if p.reachable else None, 'temp_note': p.temp_note if p.reachable else '',
            'containers': list(getattr(p, 'containers', []) or []), 'readings': getattr(p, 'readings', None),
            'owner_paused': m.get('paused'), 'use_pct': pct, 'use_why': pct_why, 'limit': m.get('limit'),
            'limited': share.limited(m, now) is not None, 'zone': getattr(p, 'zone', '') if p.reachable else '',
            'mem_free_gb': getattr(p, 'mem_gb', None) if p.reachable else None}


def machines_state(cfg, probe_fn, stored_fn=None):
    """Every machine probed at once (a slow ssh answer does not hold up the others). `stored_fn(machine)` lists the files
    `tutti files send` keeps there; it is asked only of reachable machines."""
    order = cfg['routing']['order']
    names = list(cfg['machines'])
    if not names:
        return []

    from . import share
    now = datetime.now(timezone.utc)
    idle = share.idle_seconds() if any(cfg['machines'][n].get('transport') == 'local' for n in names) else None
    quiet = (cfg.get('autopilot') or {}).get('quiet_hours')

    def one(n):
        m = cfg['machines'][n]
        p = probe_fn(m)
        here = idle if m.get('transport') == 'local' else getattr(p, 'idle_s', None)   # remote: read by its probe
        out = machine_state(m, p, order.index(n) + 1 if n in order else None, now, here, quiet)
        out['idle_s'] = here
        out['stored'] = stored_fn(m) if stored_fn and p.reachable else None
        return out
    with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
        return list(pool.map(one, names))


# ---- workers


def agents_state(cfg, which=shutil.which, serving=False):
    """The known CLI agents: installed here or not, and whether a worker in the config uses them. Ollama also counts as
    there when a configured model server answers (it may run in a container or on another machine behind a tunnel)."""
    used = {w['command'][0] for w in cfg['workers'].values() if w.get('kind') == 'cli'}
    used |= {'ollama'} if any(w.get('kind') == 'ollama' for w in cfg['workers'].values()) else set()
    return [{'command': c, 'label': label, 'installed': bool(which(c)) or (c == 'ollama' and serving), 'configured': c in used}
            for c, label in KNOWN_AGENTS]


def account_of_worker(cfg, name):
    """The account a worker spends: the one of the same name, else the first that says `worker: <name>`."""
    if name in cfg['accounts']:
        return name
    return next((n for n, a in cfg['accounts'].items() if (a or {}).get('worker') == name), None)


def worker_accounts(cfg):
    """Accounts that name a worker, in `routing.workers` order (by worker) when given, else in config order."""
    names = [n for n, a in cfg['accounts'].items() if (a or {}).get('worker')]
    order = cfg['routing'].get('workers') or []
    pos = {w: i for i, w in enumerate(order)}
    names = [n for n in names if not order or cfg['accounts'][n]['worker'] in pos]
    return sorted(names, key=lambda n: pos.get(cfg['accounts'][n]['worker'], 0))


def next_worker(cfg, accounts):
    """Where the next agent job goes, from the latest readings: {worker, account, args, reason}, worker None when none has room."""
    by_name = {a['name']: a for a in accounts}
    states = [by_name[n] for n in worker_accounts(cfg) if n in by_name]
    best, reason = routing.pick_account(states)
    if not best:
        return {'worker': None, 'account': None, 'args': [], 'reason': reason}
    acc = cfg['accounts'][best['name']]
    from . import tiers
    choice = tiers.choose(cfg, best['name'], best)   # stronger while behind its binding limit, cheaper while ahead
    return {'worker': acc['worker'], 'account': best['name'], 'args': choice['args'], 'reason': f'{reason}; {choice["label"]}'}


def with_readers(usage, readers):
    """The accounts with what their automatic readers said: the plan label, how often they are read, and a problem if any."""
    for a in usage['accounts']:
        r = (readers or {}).get(a['name']) or {}
        a.update(plan=r.get('plan'), auto_every_minutes=r.get('every_minutes'), reader_problem=r.get('problem'))
    return usage


def workers_state(cfg, available_fn, which=shutil.which):
    checks = workers.statuses(cfg, available_fn)   # the same status `tutti workers` prints
    items = []
    for name, w in cfg['workers'].items():
        ok, note = checks[name]
        items.append({'name': name, 'kind': w['kind'], 'ready': bool(ok), 'note': note, 'model': w.get('model'),
                      'command': (w.get('command') or [None])[0], 'machine': w.get('machine'),
                      'account': account_of_worker(cfg, name)})
    serving = any(w['ready'] for w in items if w['kind'] == 'ollama')
    return {'items': items, 'agents': agents_state(cfg, which, serving)}


# ---- jobs


def tail(path, lines=TAIL_LINES):
    """The last lines of a log, without terminal colour codes and with very long lines cut."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            text = f.read().decode('utf8', errors='replace')
    except OSError:
        return []
    out = [ANSI.sub('', line).replace('\r', '')[:TAIL_WIDTH] for line in text.splitlines()[-lines:]]
    return out


def progress_of_line(line):
    for source, pattern in PROGRESS:
        m = pattern.search(line)
        if not m:
            continue
        if source == 'queue':
            return {'done': 0, 'total': int(m.group(1)), 'pct': 0, 'source': 'queue'}
        if source == 'percent':
            pct = int(m.group(1))
            return {'done': pct, 'total': 100, 'pct': pct, 'source': 'log'} if pct <= 100 else None
        done, total = int(m.group(1)), int(m.group(2))
        if 0 < total and done <= total:
            return {'done': done, 'total': total, 'pct': round(100 * done / total), 'source': 'log'}
    return None


def read_progress_file(path):
    """`tutti progress n/m` (or a job's own script) writes `<n> <m>` to <log>.progress; it beats the log's lines. A third
    word is the unit (a file transfer writes `<done> <total> MB`)."""
    try:
        with open(path, encoding='utf8') as f:
            words = f.read().replace('/', ' ').split()
        done, total = (int(x) for x in words[:2])
    except (OSError, ValueError):
        return None
    if total <= 0 or not 0 <= done <= total:
        return None
    out = {'done': done, 'total': total, 'pct': round(100 * done / total), 'source': 'reported'}
    if len(words) > 2 and words[2].isalpha():
        out['unit'] = words[2][:8]
    return out


def progress(log_path, lines):
    """{done, total, pct, source} or None when nothing says how far the job is (the page then shows a dash, never a guess)."""
    reported = read_progress_file(log_path + '.progress')
    if reported:
        return reported
    for line in reversed(lines):
        if SLOT_START.search(line):
            return None   # out of the queue and nothing counted since
        p = progress_of_line(line)
        if p:
            return p
    return None


def alive(pid):
    try:
        return not waiting.pid_gone(int(pid))()
    except (ValueError, OSError):
        return False


def read_rc(path):
    try:
        with open(path, encoding='utf8') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def read_first_line(path):
    try:
        with open(path, encoding='utf8', errors='replace') as f:
            return f.readline().strip()[:200]
    except OSError:
        return ''


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def status_of(running, updated, rc, now, stall):
    if running:
        return 'stalled' if updated and now - updated >= stall else 'running'
    if rc is None:
        return 'ended'
    return 'done' if rc == 0 else 'failed'


LOGS = '*.log'
JOB_TITLES = {'quiet': 'quiet run', 'ship': 'ship check', 'files': 'file transfer', 'run': 'delegated run', 'test': 'test run',
              'device': 'device run'}


def log_job(path, kind, now, stall, is_alive):
    m = LOG_NAME.match(os.path.basename(path))
    started, pid = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    pid = read_rc(path + '.pid') or pid   # a background run names its own process (the starter's pid is in the name)
    # A progress report is activity too: a long copy that only moves its byte counter is not stalled.
    updated = max((t for t in (mtime(path), mtime(path + '.progress')) if t), default=None)
    # The result first: a ship check's pid is the ship process, which lives on through later checks and the deploy.
    rc = read_rc(path + '.rc')
    running = rc is None and bool(pid) and is_alive(pid)
    title = read_first_line(path + '.cmd') or JOB_TITLES[kind]
    lines = tail(path)
    status = status_of(running, updated, rc, now, stall)
    prog = progress(path, lines)
    if status == 'done' and not (prog and prog.get('unit')):
        prog = {'done': 1, 'total': 1, 'pct': 100, 'source': 'finished'}
    return {'id': f'{kind}:{os.path.basename(path)}', 'kind': kind, 'title': title, 'pid': pid, 'log': path, 'started': iso(started),
            'updated': iso(updated), 'idle_seconds': round(now - updated) if updated else None, 'rc': rc,
            'ended': iso(mtime(path + '.rc')) if rc is not None else None,   # the result file is written when it ends
            'status': status, 'tail': lines, 'progress': prog, 'handoff': read_first_line(path + '.handoff')}


def wait_jobs(folder, now, stall, is_alive):
    out = []
    for path in glob.glob(os.path.join(folder, '*.json')):
        pid = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, encoding='utf8') as f:
                info = json.load(f)
        except (OSError, ValueError):
            continue
        if not is_alive(pid):
            continue
        out.append(wait_job(pid, info, now, stall))
    return out


def wait_job(pid, info, now, stall):
    watch = info.get('watch') or info.get('target', '')
    updated = mtime(watch) if os.path.exists(watch) else None
    limit = info.get('stall') or stall
    target = f'{info.get("kind", "?")} {info.get("target", "")} {info.get("pattern", "")}'.strip()
    job = {'id': f'wait:{pid}', 'kind': 'wait', 'title': f'wait {target}', 'pid': int(pid), 'log': None,
           'started': iso(info.get('started')), 'updated': None, 'idle_seconds': None, 'rc': None,
           'status': status_of(True, updated, None, now, limit), 'tail': [], 'progress': None}
    if updated:
        job.update(log=watch, updated=iso(updated), idle_seconds=round(now - updated), tail=tail(watch, 12),
                   progress=progress(watch, tail(watch)))
    return job


def pid_jobs(folder, is_alive):
    """Local-model jobs (llm-busy-<pid>) and tunnels (tunnel-<worker>.pid) that are running now."""
    out = []
    for path in glob.glob(os.path.join(folder, 'llm-busy-*')):
        pid = path.rsplit('-', 1)[-1]
        if pid.isdigit() and is_alive(pid):
            out.append({'id': f'llm:{pid}', 'kind': 'local model', 'title': 'local-model job', 'pid': int(pid), 'log': None,
                        'started': iso(mtime(path)), 'updated': iso(mtime(path)), 'idle_seconds': None, 'rc': None, 'status': 'running',
                        'tail': [], 'progress': None})
    for path in glob.glob(os.path.join(folder, 'tunnel-*.pid')):
        name = os.path.basename(path)[len('tunnel-'):-len('.pid')]
        try:
            with open(path, encoding='utf8') as f:
                pid = f.read().strip()
        except OSError:
            continue
        if pid.isdigit() and is_alive(pid):
            log = path[:-4] + '.log'
            out.append({'id': f'tunnel:{name}', 'kind': 'tunnel', 'title': f'tunnel to {name}', 'pid': int(pid), 'log': log,
                        'started': iso(mtime(path)), 'updated': iso(mtime(log)), 'idle_seconds': None, 'rc': None, 'status': 'running',
                        'tail': tail(log, 8), 'progress': None})
    return out


def found_jobs(cfg, folder, now, stall, is_alive, known):
    """Work started outside tutti (discover.py): registered jobs, watched scripts, agent CLIs, background task outputs.
    A process already shown (a registered job, a quiet run) is not shown twice."""
    from . import discover  # discover imports this module
    seen = {j['pid'] for j in known if j.get('pid')}
    out = []
    for j in discover.registered_jobs(os.path.join(folder, 'jobs'), now, stall, is_alive):
        out.append(j)
        seen.add(j['pid'])
    try:
        procs = discover.script_jobs(cfg, now, stall)
    except OSError:
        procs = []
    out += [j for j in procs if j['pid'] not in seen]
    return out + discover.task_jobs(cfg, now)


def remote_jobs(machines):
    """The test containers the last probe saw running on each machine (they hold its slots)."""
    out = []
    for m in machines:
        for c in m.get('containers') or []:
            name, _, age = c.partition(' ')
            out.append({'id': f'remote:{m["name"]}:{name}', 'kind': 'remote', 'title': f'{name} on {m["name"]} (started {age})',
                        'machine': m['name'],
                        'pid': None, 'log': None, 'started': None, 'updated': None, 'idle_seconds': None, 'rc': None,
                        'status': 'running', 'tail': [], 'progress': None})
    return out


ORDER = {'stalled': 0, 'running': 1, 'failed': 2, 'ended': 3, 'done': 4}


def jobs_state(cfg, now, is_alive=alive):
    """Running, stalled and recent jobs from the state folder: quiet runs, ship checks, file transfers, waits, local-model jobs, tunnels."""
    folder = config.expand(cfg['project']['state'])
    stall = float(cfg['dashboard']['stall_minutes']) * 60
    items = [log_job(p, 'quiet', now, stall, is_alive) for p in glob.glob(os.path.join(folder, 'quiet', LOGS))]
    items += [log_job(p, 'ship', now, stall, is_alive) for p in glob.glob(os.path.join(folder, 'ship-*.log'))]
    items += [log_job(p, 'files', now, stall, is_alive) for p in glob.glob(os.path.join(folder, 'files', LOGS))]
    items += [log_job(p, 'run', now, stall, is_alive) for p in glob.glob(os.path.join(folder, 'runs', LOGS))]
    items += [log_job(p, 'test', now, stall, is_alive) for p in glob.glob(os.path.join(folder, 'tests', LOGS))]
    items += [log_job(p, 'device', now, stall, is_alive) for p in glob.glob(os.path.join(folder, 'devices', LOGS))]
    items += wait_jobs(os.path.join(folder, 'waits'), now, stall, is_alive)
    items += pid_jobs(folder, is_alive)
    items += found_jobs(cfg, folder, now, stall, is_alive, items)
    items.sort(key=lambda j: (ORDER.get(j['status'], 9), -(datetime.fromisoformat(j['updated']).timestamp() if j['updated'] else 0)))
    return {'folder': folder, 'stall_seconds': stall, 'items': items[:MAX_JOBS], 'total': len(items)}


# ---- the whole state


def summary(usage, machines, workers, jobs, nxt=None):
    accounts = usage['accounts']
    return {'next_worker': nxt, 'panic': [a['name'] for a in accounts if a['panic']],
            'behind': [a['name'] for a in accounts if a['verdict'] in ('behind', 'late')],
            'free_slots': sum(m['free'] or 0 for m in machines), 'slots': sum(m['slots'] for m in machines),
            'machines_offline': [m['name'] for m in machines if m['status'] == 'offline'],
            'workers_ready': sum(1 for w in workers['items'] if w['ready']), 'workers': len(workers['items']),
            'running': sum(1 for j in jobs['items'] if j['status'] == 'running'),
            'outside': sum(1 for j in jobs['items'] if j.get('outside')),
            'stalled': sum(1 for j in jobs['items'] if j['status'] == 'stalled')}


def assemble(cfg, probes, now=None, is_alive=alive):
    """The full /api/state answer. `probes` holds the last machines and workers results with their times (the slow parts,
    refreshed in the background): {'machines': [...], 'machines_at': ts, 'workers': {...}, 'workers_at': ts}."""
    now_dt = now or datetime.now(timezone.utc)
    now_ts = now_dt.timestamp()
    usage = usage_state(cfg, now_dt)
    machines = probes.get('machines') or []
    workers = probes.get('workers') or {'items': [], 'agents': []}
    jobs = jobs_state(cfg, now_ts, is_alive)
    from . import jobprogress  # it imports jobfacts, which this module's callers import
    extra = remote_jobs(machines) + [j for j in [jobprogress.conduct_job(cfg, is_alive)] if j]
    jobs['items'] = extra + jobs['items']
    jobs['total'] += len(extra)
    jobprogress.annotate(cfg, jobs['items'], now_ts, lambda w: account_of_worker(cfg, w) if w else None,
                         machines if probes.get('machines_at') else None)
    return {'version': __version__, 'generated_at': now_dt.isoformat(), 'config': cfg['_path'],
            'refresh_seconds': cfg['dashboard']['refresh_seconds'], 'usage': usage,
            'machines': {'probed_at': iso(probes.get('machines_at')), 'items': machines},
            'devices': {'items': probes.get('devices') or []},
            'workers': {'probed_at': iso(probes.get('workers_at')), **workers, 'off': off_notes(cfg)}, 'jobs': jobs,
            'summary': {**summary(usage, machines, workers, jobs, next_worker(cfg, usage['accounts'])), 'runs_today': runs_today(cfg)}}


def off_notes(cfg):
    """One line per worker or account that is off, for the dashboard: why, and the line that turns it on."""
    out = []
    for o in cfg.get('off') or []:
        section = 'workers' if o['kind'] == 'worker' else 'accounts'
        if o.get('paused'):
            out.append(f'{o["name"]} ({o["kind"]}): {controls.paused_note({"paused": o["paused"]})}')
            continue
        why = 'costs money per use, so it is opt-in' if o['costs_money'] else 'turned off'
        out.append(f'{o["name"]} ({o["kind"]}): {why}; {section}.{o["name"]}.enabled: true in the config turns it on')
    return out


def runs_today(cfg):
    from . import delegate  # delegate imports modules that import this one
    return delegate.runs_today(cfg)


def probe_all(cfg, probe_fn, available_fn, which=shutil.which, clock=time.time, stored_fn=None, readings_fn=None):
    """The slow part, run off the request path: every machine (and the files it stores) and every worker."""
    return {'machines': machines_state(cfg, probe_fn, stored_fn), 'machines_at': clock(),
            'devices': devices.devices_state(cfg, readings_fn or (lambda m, d: {'state': 'unreachable'})),
            'workers': workers_state(cfg, available_fn, which), 'workers_at': clock()}
