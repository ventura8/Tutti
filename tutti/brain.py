"""Tutti's own AI, local first (backlog t80): every internal use of a model (a needs-question's wording, the welcome-back
summary, Sentry triage, a scout's draft, task sizing, chat compaction, self-improvement proposals, the unblocker's
diagnosis) goes through `ask`, and only through it (tests/test_brain.py fails a module that calls a model directly).

- **The best free local model, household-wide.** Every ollama worker's machine is read from the fleet reading
  (tutti/fleet.py, refreshed after every probe), so a model that appears on any machine (the owner's or a member's that
  shares) is a candidate at the next call, and one that disappears is gone. Candidates are ranked by measured capability:
  the model's tier and parameters scaled by its quantisation (`capability`), with fleet.score's per-kind bonus for the
  purpose's task type. No small default wins while a larger model is free.
- **Free within a second.** A machine whose yield flag is up, a server that does not answer within a second, or one already
  running another model is skipped at once, never waited for.
- **Online only as the fallback.** `ai_assist.online_worker` names a CLI worker, or `auto`: the first subscription worker
  (no per-use cost) whose account has room within pacing (review.worker_room). Online text is scrubbed (aiassist.online_text).
- **Every use is logged** (`brain-use.json`: purpose, local or online, model, machine, seconds, watt-hours) for the dashboard's
  "Tutti's own AI" card.

Pure: capability, rank, summary. The edge: probes, the model call, the log."""
import json
import os
import re
import threading
import time

from . import config

FILE = 'brain-use.json'
KEEP = 500
PURPOSE_KIND = {'triage': 'triage', 'sizing': 'fix', 'unblock': 'fix', 'conductor': 'fix', 'review': 'review',
                'compaction': 'summary', 'welcome': 'summary', 'needs': 'summary', 'scout': 'summary', 'self-improve': 'summary'}
QUANT_BITS = re.compile(r'(?:^|[^a-z])(?:q|iq|f|bf)(\d+)', re.I)
_lock = threading.Lock()


# ---- ranking (pure)

def quant_factor(quant):
    """How much of a model's size its quantisation keeps: 8 bits or more 1.0, 5-6 bits 0.95, 4 bits 0.9, fewer 0.8; unknown 0.9."""
    hit = QUANT_BITS.search(str(quant or ''))
    bits = int(hit.group(1)) if hit else 4
    for least, factor in ((8, 1.0), (5, 0.95), (4, 0.9)):
        if bits >= least:
            return factor
    return 0.8


def capability(model, purpose='other'):
    """A comparable number: fleet.score for the purpose's task kind with the parameters scaled by the quantisation."""
    from . import fleet
    kind = PURPOSE_KIND.get(purpose, 'summary')
    p = float(model.get('params_b') or 0)
    return fleet.score(model, kind) - p + p * quant_factor(model.get('quant'))


def as_model(name):
    from . import fleet
    return fleet.classify({'name': name}) or {'name': name, 'params_b': 0.0, 'tier': 'normal', 'kinds': [], 'code': False}


def candidates(cfg, fleet_state=None):
    """[(worker name, machine, model dict)] for every model every enabled ollama worker's machine has now (the fleet
    reading), else the worker's own model."""
    from . import fleet
    machines = (fleet_state if fleet_state is not None else fleet.read_state(cfg)).get('machines') or {}
    out, seen = [], set()
    for name, w in (cfg.get('workers') or {}).items():
        if not is_local(w):
            continue
        machine = fleet.machine_of(cfg, name)
        for m in models_of(w, machines.get(machine) or {}):
            if (machine, m['name']) not in seen:
                seen.add((machine, m['name']))
                out.append((name, machine, m))
    return out


def is_local(w):
    return isinstance(w, dict) and w.get('kind') == 'ollama' and w.get('enabled') is not False


def models_of(w, entry):
    """The machine's models now (its fresh fleet entry), else the worker's last known ones, else its own model."""
    fresh = None if entry.get('stale') else entry.get('models')
    models = [m for m in fresh or w.get('models') or [] if isinstance(m, dict) and m.get('name')]
    if models or not w.get('model'):
        return models
    return [as_model(w['model'])]


def rank(cands, purpose='other'):
    """Best first: the highest capability; on a tie the larger model, then config order."""
    return sorted(cands, key=lambda c: (-capability(c[2], purpose), -float(c[2].get('params_b') or 0)))


# ---- the edge: is it free now, ask it

def others(model, ps):
    """The models the server is running other than `model` (other work's)."""
    loaded = [str(m.get('name') or m.get('model') or '') for m in (ps or {}).get('models') or []]
    tag = model if ':' in model else f'{model}:latest'
    return [] if tag in [x if ':' in x else f'{x}:latest' for x in loaded] else loaded


def free_now(cfg, worker, model, ps_fn=None, yield_fn=None):
    """('' when free, else why): yield flag up, no answer within a second, or another model running."""
    from . import local
    machine = cfg['machines'].get(worker.get('machine', ''))
    if machine and (yield_fn or local.yield_cached)(machine) is not None:
        return 'its machine yields to its owner'
    ps = (ps_fn or (lambda w: local.ollama_get(w['host'], '/api/ps', timeout=1)))(worker)
    if ps is None:
        return 'down'
    run = others(model, ps)
    return f'busy with {", ".join(run)} work' if run else ''


def preempt_allowed(cfg, purpose):
    """ai_assist.preempt: true (the default: all of Tutti's own work may pre-empt), false, or a list of purposes."""
    from . import aiassist
    v = aiassist.settings(cfg).get('preempt', True)
    return purpose in v if isinstance(v, list) else v is not False


def best_free(cfg, purpose='other', ps_fn=None, yield_fn=None, fleet_state=None, preempt=None):
    """(worker name, machine, model, [skipped lines], pre-empted work or '') of the best local model for Tutti now. A model
    busy with other work is not passed over for a smaller one when pre-emption is allowed: that work is parked instead
    (`preempted` names what was running). A yielding machine (its owner's) and a server that is down are skipped."""
    skipped = []
    preempt = preempt_allowed(cfg, purpose) if preempt is None else preempt
    for name, machine, m in rank(candidates(cfg, fleet_state), purpose):
        why = free_now(cfg, {**cfg['workers'][name], 'machine': machine}, m['name'], ps_fn, yield_fn)
        if not why:
            return name, machine, m['name'], skipped, ''
        if why.startswith('busy') and preempt:
            return name, machine, m['name'], skipped, why
        skipped.append(f'{m["name"]} on {machine}: {why}')
    return None, None, None, skipped, ''


# ---- pre-emption: Tutti's own job first on the best model; other work waits between its requests, nothing is killed

PREEMPT_MAX_S = 180   # Tutti's own job is bounded, so parking stays cheap; a stale marker older than this is ignored


def marker_dir(cfg):
    return config.state_dir(cfg)


def marker_path(cfg, machine, owner=None):
    """One marker per job of Tutti's (process and thread): two jobs on one machine never lower each other's."""
    owner = owner or f'{os.getpid()}-{threading.get_ident()}'
    return os.path.join(marker_dir(cfg), f'preempt-{re.sub(r"[^A-Za-z0-9_.-]", "_", machine or "here")}--{owner}.json')


def park_others(cfg, machine, purpose, now=None):
    """Raises this job's pre-emption marker on the machine: other work's next model request waits (local.wait_for_preempt)
    while any live marker is up, so its progress (the conversation, the worktree, the run) stays as it is."""
    p = marker_path(cfg, machine)
    with open(p, 'w', encoding='utf8') as f:
        json.dump({'purpose': purpose, 'at': now or time.time(), 'pid': os.getpid()}, f)
    return p


def preempted_now(cfg, machine, now=None):
    """A live marker's record (one not older than PREEMPT_MAX_S) on the machine, else None."""
    import glob
    prefix = f'preempt-{re.sub(r"[^A-Za-z0-9_.-]", "_", machine or "here")}--'
    for p in glob.glob(os.path.join(glob.escape(marker_dir(cfg)), prefix + '*.json')):
        try:
            with open(p, encoding='utf8') as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if (now or time.time()) - float(rec.get('at') or 0) < PREEMPT_MAX_S:
            return rec
    return None


def resume_others(cfg, machine):
    try:
        os.remove(marker_path(cfg, machine))
    except OSError:
        pass


def online_pick(cfg, available=None, room=None):
    """(worker name, args) for the online fallback or (None, why). `auto`: the first subscription worker with room."""
    from . import aiassist, review, workers
    want = aiassist.settings(cfg).get('online_worker') or ''
    if not want:
        return None, 'ai_assist.online_worker is not set'
    ws = cfg.get('workers') or {}
    names = [n for n, w in ws.items() if isinstance(w, dict) and w.get('kind') == 'cli'] if want == 'auto' else [want]
    available = available or (lambda w: workers.available(w, timeout=1))
    room = room or (lambda n: review.worker_room(cfg, n, _entries(cfg), _now()))
    why = []
    for n in names:
        args, reason = online_room(ws.get(n), n, available, room)
        if args is not None:
            return n, args
        why.append(f'{n}: {reason}')
    return None, '; '.join(why) or 'no CLI worker'


def online_room(w, n, available, room):
    """(args, '') when the worker may answer now, else (None, why)."""
    if not w or w.get('kind') != 'cli' or w.get('costs_money') or w.get('enabled') is False:
        return None, 'not a subscription CLI worker'
    if not available(w)[0]:
        return None, 'not installed'
    return room(n)


def _entries(cfg):
    from . import pacing, state
    return pacing.read_log(state.usage_log_path(cfg))


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def ask_online(cfg, prompt, run=None, pick=None):
    """The online fallback, within pacing; (answer, 'online:<worker>') or (None, why)."""
    import subprocess

    from . import aiassist, workers
    if not aiassist.may_send_online(cfg, 'telemetry'):
        return None, 'online AI is off (ai_assist.online)'
    name, args = (pick or online_pick)(cfg)
    if not name:
        return None, f'no online model with room ({args})'
    argv = workers.with_args(workers.command(cfg['workers'][name], aiassist.online_text(cfg, prompt)), args)
    try:
        r = (run or subprocess.run)(argv, capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f'{name}: {type(e).__name__}'
    return (r.stdout.strip(), name) if r.returncode == 0 and r.stdout.strip() else (None, f'{name} exited {r.returncode}')


def ask(cfg, prompt, purpose='other', online=True, local_first=True, generate=None, ps_fn=None, yield_fn=None, ask_online_fn=None,
        clock=time.monotonic, fleet_state=None):
    """THE way Tutti asks a model for itself: (answer, who) from the best free local model in the household, then online
    (within pacing) when `online`; (None, why) when local AI is off or nothing answered. Every answer is logged."""
    from . import aiassist, local
    if not aiassist.switch(cfg, 'local'):
        return None, 'local AI is off (ai_assist.local)'
    generate = generate or local.generate
    tried = []
    probe = {'ps_fn': ps_fn, 'yield_fn': yield_fn, 'fleet_state': fleet_state, 'preempt': None}
    for _ in range(3 if local_first else 0):   # a model that fails: the next best one
        name, answer = ask_one(cfg, prompt, purpose, generate, probe, tried, clock)
        if answer is not None or not name:
            if answer is not None:
                return answer, name
            break
    if online:
        t0 = clock()
        answer, who = (ask_online_fn or ask_online)(cfg, prompt)
        if answer:
            record(cfg, purpose, 'online', who, '', clock() - t0)
            return answer, who
        tried.append(who)
    return None, 'no model answered (' + ('; '.join(tried) or 'no ollama worker in the config') + ')'


def ask_one(cfg, prompt, purpose, generate, probe, tried, clock):
    """One try on the best model now: (worker, answer); (worker, None) after a failure (the next try skips that model and
    never parks other work again), (None, None) when no model is free."""
    name, machine, model, skipped, preempted = best_free(cfg, purpose, probe['ps_fn'], probe['yield_fn'], probe['fleet_state'],
                                                         probe['preempt'])
    tried += [x for x in skipped if x not in tried]
    if not name:
        return None, None
    w = {**cfg['workers'][name], 'model': model, 'machine': machine}
    t0 = clock()
    if preempted:
        park_others(cfg, machine, purpose)
    try:
        answer = generate(cfg, w, prompt, timeout=PREEMPT_MAX_S if preempted else 600, own=True)
    except (RuntimeError, OSError, KeyError, ValueError) as e:
        tried.append(f'{model} on {machine}: {type(e).__name__}')
        probe['fleet_state'] = _without(cfg, probe['fleet_state'], machine, model)
        if preempted:
            probe['preempt'] = False   # other work is parked once per ask, never again after a timeout
        return name, None
    finally:
        if preempted:
            resume_others(cfg, machine)
    record(cfg, purpose, 'local', model, machine, clock() - t0, parked=preempted)
    return name, answer


def _without(cfg, fleet_state, machine, model):
    from . import fleet
    st = json.loads(json.dumps(fleet_state if fleet_state is not None else fleet.read_state(cfg)))
    e = (st.setdefault('machines', {})).get(machine)
    if e and e.get('models'):
        e['models'] = [m for m in e['models'] if m.get('name') != model]
    return st


# ---- the log and the dashboard card

def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def read_log(cfg):
    try:
        with open(path_of(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def watt_hours(cfg, machine, seconds):
    from . import fleet
    return round(fleet.watts_of(cfg, machine) * seconds / 3600, 3) if machine else 0.0


def record(cfg, purpose, where, model, machine, seconds, now=None, parked=''):
    e = {'parked': parked, 'at': now or time.time(), 'purpose': purpose, 'where': where, 'model': model, 'machine': machine,
         'seconds': round(seconds, 1), 'wh': watt_hours(cfg, machine, seconds) if where == 'local' else 0.0}
    with _lock:
        log = (read_log(cfg) + [e])[-KEEP:]
        tmp = f'{path_of(cfg)}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf8') as f:
            json.dump(log, f)
        os.replace(tmp, path_of(cfg))
    return e


def summary(log, now, days=7):
    """{local, online, wh, by_purpose: {p: {local, online}}, last} over the last `days`."""
    recent = [e for e in log if e.get('at', 0) >= now - days * 86400]
    by = {}
    for e in recent:
        where = 'online' if e.get('where') == 'online' else 'local'
        by.setdefault(e.get('purpose') or 'other', {'local': 0, 'online': 0})[where] += 1
    parks = [f"parked {e['parked'].replace('busy with ', '')} on {e['machine']} for Tutti's own job ({e['seconds']:g} s)"
             for e in recent if e.get('parked')][-5:][::-1]
    where = [e.get('where') for e in recent]
    return {'parks': parks, 'local': where.count('local'), 'online': where.count('online'),
            'wh': round(sum(float(e.get('wh') or 0) for e in recent), 2), 'by_purpose': by, 'last': recent[-1] if recent else None}


def view(cfg, now=None, ps_fn=None, yield_fn=None):
    """The dashboard card: the best free model now (one line) and the week's use."""
    name, machine, model, skipped, _p = best_free(cfg, 'other', ps_fn, yield_fn, preempt=False)
    if name:
        line = f"Tutti's own AI: {model} on {machine} (best free)"
    else:
        line = "Tutti's own AI: no local model free now" + ''.join(f' ({x})' for x in skipped[:1])
    return {'line': line, 'model': model, 'machine': machine, 'skipped': skipped[:5], **summary(read_log(cfg), now or time.time())}
