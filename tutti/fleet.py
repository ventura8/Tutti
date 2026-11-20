"""The household's local models as workers (docs/FLEET.md). Every machine's Ollama is read at each probe (its models,
their size and family, the machine's GPU, NPU and CPU), each model is classed by what it can do (small fixes, tests, docs,
translations, reviews, summaries, triage; lint-fix loops for the strong ones), and a machine with models but no configured
`ollama` worker gets one of its own (`<machine>-local`, through a self-healing tunnel when it is another machine). The
autopilot then treats them as first-class ladder steps: while the online accounts are ahead of their pace (or held by the
owner's reserve) a bounded task goes to an idle machine's model first, by capability and then by energy cost, and only a
failed local attempt escalates to a cloud account with room. The governor (heat, quiet hours, yield and pause flags, the
machine's own memory, presence) is asked before every task.

Pure: classify, kind_of, model_for, fits, overflow, order, governor, entries, workers_for. The I/O: read_state, save,
refresh (from the dashboard's readings), apply (at config load), ensure_tunnels, mem_free_gb."""
import json
import math
import os
import re
import subprocess
import time

from . import config, planner

STATE = 'fleet.json'
DEFAULT_ON = '127.0.0.1:11434'   # where Ollama listens on a machine unless energy.machines.<m>.ollama says otherwise
SUFFIX = '-local'
KINDS = ('fix', 'tests', 'docs', 'translations', 'review', 'summary', 'triage', 'lint-fix')
# What a model of each tier takes: the task kinds, the backlog sizes (a larger one is worked in steps of files_per_step files:
# the model never holds more at once) and the rank used to order the machines
TIERS = {
    # owner, 2026-11-17: local models failed most M and L tasks (t196, t221, t275 went to the PC's 27B and failed), so a
    # strong local model takes S and M, a normal one S only; larger work stays with the online accounts
    # a 60B+ model (a 70B on a 48-96 GB card, owner 2026-11-17) works like an online agent: every size
    'huge': {'kinds': KINDS, 'sizes': ('S', 'M', 'L'), 'files_per_step': 5, 'rank': 3},
    'strong': {'kinds': KINDS, 'sizes': ('S', 'M'), 'files_per_step': 3, 'rank': 2},
    'normal': {'kinds': ('fix', 'tests', 'docs', 'translations', 'review', 'summary', 'triage'), 'sizes': ('S',),
               'files_per_step': 2, 'rank': 1},
    'small': {'kinds': ('docs', 'summary', 'triage', 'translations'), 'sizes': ('S',), 'files_per_step': 1, 'rank': 0},
}
NOT_TEXT = re.compile(r'llava|vision|embed|clip|bge|nomic|minilm|whisper|-vl\b', re.I)
TRANSLATOR = re.compile(r'translat|madlad|nllb|opus-mt', re.I)
CODER = re.compile(r'coder|code|deepseek|qwen|starcoder|devstral|granite', re.I)
KIND_RULES = (   # the first that matches the task's title and files decides its kind
    ('translations', re.compile(r'translat|i18n|locale|catalog(ue)?s?\b|\.po\b|ltrans', re.I)),
    ('lint-fix', re.compile(r'\blint|ruff|eslint|stylelint|mypy|\btsc\b|type error|prettier|format check|\b[EFW]\d{3}\b', re.I)),
    ('review', re.compile(r'\breview', re.I)),
    ('summary', re.compile(r'summar', re.I)),
    ('triage', re.compile(r'triage|sentry|crash report', re.I)),
    ('tests', re.compile(r'\btests?\b|coverage|spec\b|unit test', re.I)),
    ('docs', re.compile(r'\bdocs?\b|readme|docstring|changelog|\.md\b', re.I)),
)
UNITS_ANY = 3   # a free local worker has no quota: its room fits any size; fits() and the step loop decide what it takes
SIZE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*([BM])', re.I)


# ---- models and tasks (pure)


def params_b(m):
    """The model's parameters in billions: details.parameter_size ('12.2B', '494M'), else its name's tag (qwen3.5:9b), else 0."""
    name = m.get('name') or ''
    for text in ((m.get('details') or {}).get('parameter_size') or '', name.split(':')[-1] if ':' in name else ''):
        hit = SIZE_RE.search(text)
        if hit:
            n = float(hit.group(1))
            return n / 1000 if hit.group(2).upper() == 'M' else n
    return 0.0


def classify(m):
    """One /api/tags model as {name, params_b, family, tier, kinds, code, size_gb}, or None for a model that does not write
    text (vision, embeddings, speech)."""
    name = m.get('name') or m.get('model') or ''
    if not name or NOT_TEXT.search(name):
        return None
    p = params_b(m)
    # a size it does not show: a usual 7-9B
    tier = 'huge' if p >= 60 else 'strong' if p >= 14 else 'normal' if p >= 7 or not p else 'small'
    kinds = ('translations',) if TRANSLATOR.search(name) else TIERS[tier]['kinds']
    return {'name': name, 'params_b': p, 'family': (m.get('details') or {}).get('family') or '', 'tier': tier,
            'kinds': list(kinds), 'code': bool(CODER.search(name)) and not TRANSLATOR.search(name),
            'size_gb': round((m.get('size') or 0) / 2 ** 30, 1), 'quant': (m.get('details') or {}).get('quantization_level') or ''}


def kind_of(task):
    """The task's kind (one of KINDS) from its title, notes and files; a plain change is a `fix`."""
    files = task.get('scope') or []
    text = ' '.join([task.get('title') or '', *files])
    for kind, rule in KIND_RULES:
        if rule.search(text):
            return kind
    if files and all(f.endswith('.md') for f in files):
        return 'docs'
    return 'fix'


def score(model, kind):
    """How well a model suits a kind: its tier first, a coder for code work, a translator for translations, then its size."""
    bonus = 0.0
    if kind == 'translations' and 'translations' in model['kinds'] and len(model['kinds']) == 1:
        bonus = 150.0   # a translator beats any general model at translations
    elif kind in ('fix', 'tests', 'lint-fix', 'review') and model.get('code'):
        bonus = 5.0
    return TIERS[model['tier']]['rank'] * 100 + bonus + model['params_b']


def model_for(worker, task, rates=None):
    """The worker's model that takes this task ({name, tier, ...}) or None: its kind and size must fit the model's tier. Among
    those, the weakest measured tier that meets the task's difficulty (tutti/power.py: size, plus one per failed attempt), so
    the strong models stay free for hard work; the configured model wins a tie, else the best scored."""
    from . import power
    kind, size = kind_of(task), task.get('size') or 'S'
    models = worker.get('models') or [classify({'name': worker['model']}) or {}]
    ok = [m for m in models if m and kind in m['kinds'] and size in TIERS[m['tier']]['sizes']]
    if not ok:
        return None
    ok = [m for m in ok if m['kinds'] == ['translations']] if kind == 'translations' and any(
        m['kinds'] == ['translations'] for m in ok) else ok   # a translator beats any general model at translations
    need = power.difficulty(task)
    fit = min(power.prefer(power.model_tier(m, rates)[0], need) for m in ok)
    ok = [m for m in ok if power.prefer(power.model_tier(m, rates)[0], need) == fit]
    own = next((m for m in ok if m['name'] == worker.get('model')), None)
    best = max(ok, key=lambda m: score(m, kind))
    return own if own and score(own, kind) >= score(best, kind) - 10 else best


def fits(worker, task):
    """('', model) when the worker can take the task, else (why not, None)."""
    m = model_for(worker, task)
    if not m:
        names = ', '.join(x['name'] for x in worker.get('models') or [] if x) or worker.get('model')
        return f'no model here ({names}) takes a {task.get("size") or "S"} {kind_of(task)} task', None
    return '', m


def files_per_step(model):
    return TIERS[(model or {}).get('tier', 'small')]['files_per_step']


def steps(files, per):
    """A task's files in steps a local model can hold at once: `per` files each (at least 1)."""
    per = max(1, int(per or 1))
    files = list(files)
    return [files[i:i + per] for i in range(0, len(files), per)] or [[]]


STOP = {'the', 'and', 'for', 'with', 'from', 'that', 'this', 'when', 'into', 'than', 'then', 'each', 'every', 'only', 'make',
        'tutti', 'should', 'instead', 'today', 'about', 'calls', 'call', 'add', 'fix', 'not', 'one', 'its', 'are', 'was'}
SOURCE = re.compile(r'\.(py|ts|js|mjs|tsx|sh|md|json|yaml|yml|css|html)$')


def words(title):
    """The title's words (a plural also as its singular: worktrees names worktree.py)."""
    out = {w for w in re.findall(r'[a-z][a-z0-9_]{2,}', (title or '').lower()) if w not in STOP}
    return out | {w[:-1] for w in out if w.endswith('s') and len(w) > 4}


def guess_scope(title, files, limit=3):
    """The files a task without scope most likely changes (so a local model, which edits only the files it is given, can take
    it): the source files whose name is a word of the title, best first, at most `limit`; [] when none is named."""
    named = [f for f in files if f in (title or '').split() or f'{f}:' in (title or '') or f'`{f}`' in (title or '')]
    if named:
        return named[:limit]
    want = words(title)
    scored = []
    for f in files:
        if not SOURCE.search(f) or '/test' in f or f.startswith('test') or 'node_modules/' in f:
            continue
        stem = re.sub(r'\.[a-z]+$', '', f.rsplit('/', 1)[-1]).lower()
        hit = 3 * (stem in want) + sum(1 for part in f.lower().split('/')[:-1] if part in want)
        if hit >= 3:
            scored.append((-hit, f.endswith('.md'), len(f), f))   # code before docs, then the shorter path
    return [x[-1] for x in sorted(scored)[:limit]]


def task_files(task):
    """The files a local model works on: the task's scope, else the files Tutti guessed for it (local_scope)."""
    return list(task.get('scope') or task.get('local_scope') or [])


# ---- overflow and order (pure)


def overflow(cfg, states, rooms, local_names):
    """Why the online accounts should be spared now, or '': an account of the ladder or of autopilot.accounts is ahead of its
    pace (or in panic, or spent), or no trigger account has room (its pace, or the owner's reserve keeps it)."""
    from . import autopilot
    names = [n for n in dict.fromkeys([*autopilot.ladder(cfg), *autopilot.trigger_accounts(cfg)])
             if n not in local_names and n in cfg['accounts']]
    ahead = [n for n in names if (states.get(n) or {}).get('verdict') in ('ahead', 'panic', 'spent') or (states.get(n) or {}).get('panic')]
    if ahead:
        return f'{", ".join(ahead)} ahead of pace'
    trig = [n for n in autopilot.trigger_accounts(cfg) if n in cfg['accounts']]
    if trig and sum(max(0, (rooms.get(n) or (0, ''))[0]) for n in trig) <= 0:
        return 'no online account has room (its pace or the owner\'s reserve)'
    return ''


def watts_of(cfg, machine):
    """A rough draw of the machine while a model runs (energy.machines.<m>: base + GPU + CPU TDP) to order by energy cost."""
    m = ((cfg.get('energy') or {}).get('machines') or {}).get(machine) or {}
    return float(m.get('base_w') or 0) + float(m.get('gpu_tdp_w') or 150) + float(m.get('cpu_tdp_w') or 0)


def order(cfg, names, task, machines, rates=None):
    """Local steps that can take the task, best first: an idle machine, then the model tier that suits the task's difficulty
    (tutti/power.py: the weakest tier that meets it, so hard work finds the strong models free; a failure escalates), then
    for easy work the weaker machine and for hard work the stronger one (measured power), then the lowest energy cost."""
    from . import power
    rows = {m.get('name'): m for m in machines or ()}
    need = power.difficulty(task)
    rates = power.rates(cfg) if rates is None else rates
    out = []
    for n in names:
        w = cfg['workers'][n]
        model = model_for(w, task, rates)
        if not model:
            continue
        home = machine_of(cfg, n)
        row = rows.get(home) or {}
        idle = row.get('status') == 'free'
        strength = float((row.get('power') or {}).get('score') or 0)
        out.append(((not idle, power.prefer(power.model_tier(model, rates)[0], need), strength if need < 2 else -strength,
                     watts_of(cfg, home)), n))
    return [n for _, n in sorted(out)]


def machine_of(cfg, name):
    """The machine a worker's model runs on: workers.<w>.machine, else this computer's machine (transport local)."""
    w = cfg['workers'].get(name) or {}
    return w.get('machine') or next((n for n, m in cfg['machines'].items() if m.get('transport') == 'local'), '')


HOT_OR_LOADED = re.compile(r'^(too hot|hot): |^load [\d.]+ is over')   # routing.heat_unfit and unfit's load ratio
BLOCKING = {'held': 'paused by the owner', 'off': 'turned off', 'offline': 'offline', 'paused': 'its yield flag is up'}


def governor(cfg, name, machines, now=None, mem_gb=None, hardware=None):
    """Why the worker's machine may not run a model now, or '': owner pause, offline (presence), its yield flag (the PC yields
    to RoRadio), heat or load, its own quiet hours (machines.<m>.quiet_hours), too little free memory (fleet.memory_floor_gb,
    read on this computer), and a CPU-only machine whose test slots are all taken."""
    home = machine_of(cfg, name)
    row = next((m for m in machines or () if m.get('name') == home), None)
    if row:
        st, reason = row.get('status'), row.get('reason') or ''
        if st in BLOCKING:
            return f'{name}: its machine {home} is {BLOCKING[st]} ({reason})'
        if st == 'loaded' and HOT_OR_LOADED.search(reason):
            return f'{name}: its machine {home} is {reason}'
        if st == 'busy' and not (hardware or {}).get('gpu'):
            return f'{name}: its machine {home} runs on its CPU and every slot is busy'
    hours = (cfg['machines'].get(home) or {}).get('quiet_hours')
    if now is not None and hours and planner.quiet(now, hours):
        return f'{name}: quiet hours on {home} ({", ".join(hours)}, machines.{home}.quiet_hours)'
    floor = float(settings(cfg).get('memory_floor_gb') or 0)
    if mem_gb is not None and mem_gb < floor:
        return f'{name}: {home} has {mem_gb:.1f} GB of memory free, under fleet.memory_floor_gb {floor:g}'
    return ''


# ---- discovery (pure over the readings)


def settings(cfg):
    return cfg.get('fleet') or {}


def entries(cfg, readings, now, old=None):
    """{machine: {models, gpu, npu, cpu, loaded, updated}} from the dashboard's readings (energy.probe_all: each machine's
    /api/tags and /api/ps, GPU name). A machine that did not answer keeps its last entry, marked `stale`."""
    old = {name: {**entry, 'tags_endpoint': config.safe_ollama_endpoint(entry.get('tags_endpoint'))}
           for name, entry in (old or {}).items() if isinstance(entry, dict)}
    out = {}
    for name, r in (readings or {}).items():
        tags = (r or {}).get('tags')
        if r is None or not isinstance(tags, dict) or not isinstance(tags.get('models'), list):
            if name in old:
                out[name] = {**old[name], 'stale': True}
            continue
        models = [c for c in (classify(m) for m in tags['models'] if isinstance(m, dict)) if c]
        loaded = [m.get('name') for m in ((r.get('ps') or {}).get('models') or []) if isinstance(m, dict)]
        out[name] = {'models': models, 'gpu': r.get('gpu') or '', 'npu': r.get('npu') or '', 'cpu': r.get('cpu') or '',
                     'loaded': loaded, 'updated': now, 'stale': False,
                     'tags_endpoint': config.safe_ollama_endpoint(r.get('tags_endpoint'))}
    for name, e in old.items():
        if config.physical(cfg, name) == name:   # a dedicated pseudo-machine's old entry goes: its host's models are the same
            out.setdefault(name, {**e, 'stale': True})
    return out


def num_ctx(models, gpu):
    """The context a machine's models get: 32k with a GPU, 16k on a CPU (an Arc iGPU or an NPU counts as a CPU here)."""
    return 32768 if gpu and re.search(r'nvidia|geforce|rtx|radeon|amd', gpu, re.I) else 16384


def workers_for(cfg, fleet):
    """{name: worker} to add: a `<machine>-local` ollama worker for every machine whose models no configured worker reaches.
    Another machine's server is reached through a tunnel on fleet.port_base + its index (`tutti tunnel start <worker>`)."""
    taken = {machine_of(cfg, n) for n, w in cfg['workers'].items() if w.get('kind') == 'ollama' and not w.get('auto')}
    out = {}
    base = int(settings(cfg).get('port_base') or 11450)
    for i, (name, e) in enumerate(sorted(fleet.items())):
        m = cfg['machines'].get(name)
        usable = [x for x in e.get('models') or [] if x]
        if not m or name in taken or not usable or m.get('transport') in ('android', 'household'):
            continue
        on = (((cfg.get('energy') or {}).get('machines') or {}).get(name) or {}).get('ollama') or DEFAULT_ON
        best = max(usable, key=lambda x: score(x, 'fix'))
        w = {'kind': 'ollama', 'model': best['name'], 'machine': name, 'auto': True, 'models': usable,
             'num_ctx': num_ctx(usable, e.get('gpu')), 'hardware': hardware(e)}
        if m.get('transport') == 'local':
            w['host'] = on
        else:
            w['host'] = f'127.0.0.1:{base + i}'
            w['tunnel'] = {'ssh': m.get('host'), 'remote': on}
        out[name + SUFFIX] = w
    return out


def hardware(e):
    return {'gpu': e.get('gpu') or '', 'npu': e.get('npu') or '', 'cpu': e.get('cpu') or ''}


def enrich(cfg, fleet):
    """The configured ollama workers learn their machine's installed models and hardware (their own model stays first)."""
    for n, w in cfg['workers'].items():
        if w.get('kind') != 'ollama' or w.get('auto'):
            continue
        e = fleet.get(machine_of(cfg, n))
        if e:
            w['models'] = [x for x in e.get('models') or [] if x]
            w['hardware'] = hardware(e)


def local_names(cfg):
    return [n for n, w in cfg['workers'].items() if w.get('kind') == 'ollama' and w.get('enabled') is not False]


# ---- state and I/O


def path(cfg):
    return os.path.join(config.state_dir(cfg), STATE)


def read_state(cfg):
    try:
        with open(path(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(cfg, data):
    tmp = f'{path(cfg)}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path(cfg))


def refresh(cfg, readings, now):
    """After each probe: the machines' models and hardware stored; new or removed models change the workers at the next load."""
    data = read_state(cfg)
    data['machines'] = entries(cfg, readings, now, data.get('machines'))
    data['speeds'] = speeds(cfg, now)
    save(cfg, data)
    return data


def speeds(cfg, now):
    """{model: tokens per second} measured over the last 30 days of local runs (Ollama's own eval count and time)."""
    from . import energy
    sums = {}
    for r in energy.read_runs(cfg, now - 30 * 86400):
        if r.get('model') and r.get('eval_s'):
            s = sums.setdefault(r['model'], [0, 0.0])
            s[0] += r.get('out') or 0
            s[1] += r['eval_s']
    return {m: round(o / t, 1) for m, (o, t) in sums.items() if t > 0}


def apply(cfg):
    """At config load: the discovered workers added and the configured ones enriched (fleet.enabled: false turns it off)."""
    if settings(cfg).get('enabled') is False:
        return cfg
    data = read_state(cfg)
    fleet = data.get('machines') or {}
    enrich(cfg, fleet)
    found = workers_for(cfg, fleet)
    for name in [n for n, w in cfg['workers'].items() if w.get('auto') and n not in found]:
        del cfg['workers'][name]   # its machine has no usable model any more
    for name, w in found.items():
        if name not in cfg['workers'] or cfg['workers'][name].get('auto'):
            cfg['workers'][name] = w   # new or changed models: the worker follows them
    sp = data.get('speeds') or {}
    for w in cfg['workers'].values():
        for m in w.get('models') or []:
            if m and m['name'] in sp:
                m['tokens_s'] = sp[m['name']]
    return cfg


def mem_free_gb(meminfo='/proc/meminfo'):
    """This computer's available memory in GB, or None when it cannot be read."""
    try:
        with open(meminfo, encoding='utf8') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) / 2 ** 20
    except (OSError, ValueError, IndexError):
        return None
    return None


def ensure_tunnels(cfg, machines, answers, run=subprocess.run):
    """Starts the tunnel of every ollama worker that has one, whose server does not answer and whose machine is up. `answers`:
    {worker: bool}. Returns the workers whose tunnel was started."""
    from . import procs
    rows = {m.get('name'): m for m in machines or ()}
    started = []
    # tests and checks: no real tunnel loop (an e2e test host's ran for hours, 2026-11-15)
    if run is subprocess.run and not procs.daemons_allowed():
        return started
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', 'tunnel.sh')
    for n, w in cfg['workers'].items():
        t = w.get('tunnel') or {}
        home = rows.get(machine_of(cfg, n)) or {}
        if w.get('kind') != 'ollama' or not t.get('ssh') or answers.get(n) is not False or home.get('status') in BLOCKING:
            continue   # answers None: not probed yet
        env = {**os.environ, 'TUTTI_STATE': config.state_dir(cfg)}
        try:
            r = run([script, 'start', n, w['host'], t['ssh'], t['remote']], env=env, capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if getattr(r, 'returncode', 0) == 0:
            started.append(n)
    if started:
        data = read_state(cfg)
        data['woke'] = {'at': time.time(), 'workers': started}
        save(cfg, data)
    return started


RESTART_EVERY = 600   # seconds between two restarts of one machine's Ollama
# Ollama again on a machine: its user service, else `ollama serve` in the background; a system service needs the owner's sudo
RESTART_SCRIPT = ('if systemctl --user restart ollama 2>/dev/null; then echo user-service; '
                  'elif systemctl is-enabled ollama >/dev/null 2>&1; then echo system-service; '
                  'elif command -v ollama >/dev/null; then (setsid nohup ollama serve >/dev/null 2>&1 &); echo serve; '
                  'else echo none; fi')


def server_down(e):
    """A previously observed model server did not answer this probe."""
    return bool(e.get('stale') and e.get('models'))


def restart_ready(row, observed_at, now):
    """Conservatively defer on busy, loaded, stale or incomplete machine observations."""
    if (not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool) or not math.isfinite(observed_at)
            or not 0 <= now - observed_at <= 120):
        return False
    if not isinstance(row, dict) or row.get('status') != 'free' or row.get('reachable') is not True or row.get('stale'):
        return False
    busy, free, load, cores = (row.get(k) for k in ('busy', 'free', 'load', 'cores'))
    values = (busy, free, load, cores)
    return (all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)
            and busy == 0 and free > 0 and 0 <= load < cores and cores > 0)


def restart_fix(machine, how):
    """The one-step fix the owner sees when Tutti could not restart a machine's Ollama itself."""
    if machine.get('transport') == 'wsl':
        return 'start the Ollama app on Windows (Start menu: Ollama); Tutti cannot start a Windows program from WSL'
    if how == 'unreachable':
        return f'{machine.get("name")} did not answer over ssh: check it is on and reachable (`tutti machines`)'
    if how == 'system-service':
        return f'run `sudo systemctl restart ollama` on {machine.get("name")} (a system service needs your password)'
    return f'install Ollama on {machine.get("name")} (https://ollama.com/download), or start it: `ollama serve`'


def keep_servers(cfg, machines, now, sh=None, observed_at=None, clock=None):
    """Every machine whose Ollama went quiet gets a restart (its user service, else `ollama serve`), at most every
    RESTART_EVERY seconds; one that cannot be restarted by Tutti gets the exact fix. Returns the servers' state, kept in
    fleet.json: {machine: {up, since, restarted_at, how, fix}}. `clock` is re-read before each restart gate so a long
    restart cannot stretch a reading past the freshness window; tests omit it and keep `now` fixed."""
    from . import remote
    sh = sh or remote.sh
    tick = clock or (lambda: now)
    data = read_state(cfg)
    servers = data.get('servers') or {}
    rows = {m.get('name'): m for m in machines or ()}
    for name, e in (data.get('machines') or {}).items():
        now = tick()
        s = servers.setdefault(name, {'up': True, 'since': now})
        down = server_down(e)
        if not down:
            if not s.get('up'):
                s.update(up=True, since=now, how='', fix='')
            continue
        if s.get('up'):
            s.update(up=False, since=now)
        if not restart_ready(rows.get(name), observed_at, now):
            s.update(how='deferred', fix='restart deferred: wait for a fresh, free and unloaded machine reading')
            continue
        m = cfg['machines'].get(name)
        if not m or (s.get('restarted_at') is not None and now - s['restarted_at'] < RESTART_EVERY):
            continue
        how = 'wsl' if m.get('transport') == 'wsl' else _restart(sh, m)
        s.update(restarted_at=now, how=how, fix='' if how in ('user-service', 'serve') else restart_fix(m, how))
    data['servers'] = servers
    save(cfg, data)
    return servers


def _restart(sh, machine):
    try:
        r = sh(machine, RESTART_SCRIPT, capture=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return 'unreachable'
    return (r.stdout or '').strip().splitlines()[-1] if (r.stdout or '').strip() else 'none'


def after_probe(cfg, fresh, now, run=subprocess.run, sh=None):
    """The dashboard's probe round: the machines' models stored, the workers following them, a quiet Ollama restarted, every
    local worker's tunnel started when its server does not answer and its machine is up (after a drop, a reboot or sleep:
    the next probe). The household's use recorded. Returns the workers whose tunnel was started."""
    refresh(cfg, fresh.get('energy'), now)
    apply(cfg)
    from . import power
    power.after_probe(cfg, fresh.get('machines') or [], now)   # measured power and headroom on every row (dispatch reads it)
    # Batch-start age: a row finished late in a slow probe must not look fresher than when the batch began.
    started = fresh.get('machines_started_at', fresh.get('machines_at'))
    keep_servers(cfg, fresh.get('machines') or [], now, sh, observed_at=started, clock=time.time)
    ready = {w['name']: w['ready'] for w in (fresh.get('workers') or {}).get('items') or []}
    from . import householduse
    householduse.record(cfg, fresh.get('machines') or [], now)
    from . import decisional
    decisional.tick(cfg, now)   # a missing decisional model is fetched when decisional.auto_pull allows it (t440)
    return ensure_tunnels(cfg, fresh.get('machines') or [], ready, run)


def view(cfg, now=None):
    """The dashboard's and `tutti fleet`'s rows: every local worker with its machine, models, speed, hardware and kinds."""
    from . import power
    data = read_state(cfg)
    rates = power.rates(cfg)
    speed = data.get('speeds') or {}
    rows = []
    for n in local_names(cfg):
        w = cfg['workers'][n]
        rows.append({'name': n, 'machine': machine_of(cfg, n), 'auto': bool(w.get('auto')), 'model': w.get('model'),
                     'host': config.safe_ollama_endpoint(w.get('host')) or '', 'num_ctx': w.get('num_ctx'),
                     'hardware': w.get('hardware') or {},
                     'models': [{**{k: m.get(k) for k in ('name', 'tier', 'kinds', 'params_b')},
                                 'tokens_s': m.get('tokens_s') or speed.get(m.get('name')), **model_rank(m, rates)}
                                for m in w.get('models') or [] if m]})
    machines = {name: {**entry, 'tags_endpoint': config.safe_ollama_endpoint(entry.get('tags_endpoint'))}
                for name, entry in (data.get('machines') or {}).items() if isinstance(entry, dict)}
    return {'workers': rows, 'machines': machines, 'servers': data.get('servers') or {}}


def model_rank(m, rates):
    """{rank_tier, rank_why}: the measured tier (tutti/power.py: light, mid, strong) a task's difficulty is matched to."""
    from . import power
    tier, why = power.model_tier(m, rates)
    return {'rank_tier': power.TIER_NAMES[tier], 'rank_why': why}


def describe(v):
    if not v['workers']:
        return 'no local model worker: no machine\'s Ollama answered yet (the dashboard reads them at every probe)'
    lines = []
    for name, s in sorted((v.get('servers') or {}).items()):
        if not s.get('up'):
            lines.append(f'{name}: its Ollama is down' + (f'; {s["fix"]}' if s.get('fix') else '; Tutti restarted it'))
    for r in v['workers']:
        hw = r['hardware']
        where = ', '.join(x for x in (hw.get('gpu'), hw.get('npu') and f'NPU {hw["npu"]}', hw.get('cpu')) if x) or 'hardware not read'
        origin = 'auto' if r['auto'] else 'config'
        lines.append(f'{r["name"]:14} {r["machine"]:8} {origin}  {r["host"]}  ctx {r["num_ctx"] or "-"}  {where}')
        for m in r['models']:
            speed = f'{m["tokens_s"]} tok/s' if m.get('tokens_s') else 'speed not measured yet'
            lines.append(f'    {m["name"]:28} {m.get("rank_tier", m["tier"]):6} {m["params_b"]:g}B  {speed}  {", ".join(m["kinds"])}'
                         + (f'  ({m["rank_why"]})' if m.get('rank_why') else ''))
    return '\n'.join(lines)
