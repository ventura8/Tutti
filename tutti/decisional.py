"""The best free decisional model (backlog t440, docs/FLEET.md, *The decisional model*). Tutti's judgement-heavy steps (the
unblocker's diagnosis, scout triage, Needs-you classification, welcome-back summaries, routing advice) ask a model that reasons
well, not just the largest one installed. Which one is configuration: `decisional.candidates` lists free models best first, and
the plan takes the strongest suitable machine (the most graphics memory, then memory: measured by the machine benchmark,
tutti/power.py, or given in `decisional.hardware`) and the first candidate that fits it. No machine or model name lives here.

- **Pick.** `choose` ranks the hosts and the candidates; `target` is the best fit (what to pull), `usable` the best fit that is
  already installed (what to ask now). A paused machine is skipped; a machine whose hardware is unknown only counts for a model
  it already holds.
- **Route.** tutti/brain.py asks the `usable` model first for the purposes in `decisional.purposes`; when it is busy, down or
  yielding, the best free model answers as before, and the online fallback keeps its pacing. Owner questions and paid agents are
  not touched: this module only reorders the local candidates.
- **Pull.** A missing target model is fetched through that machine's model server (`/api/pull`, so a Windows or WSL machine
  works too) by `tutti decisional pull`, or by the dashboard's probe with `decisional.auto_pull` (a download of tens of GB, so
  off until allowed; never while a person is at this computer, and a failed pull waits RETRY_S). Installing Ollama itself is
  the operator's one step (docs/FLEET.md): Tutti names it when a machine has none.

Pure: settings, candidates, hosts, fit, choose, pull_blocker, advice_prompt, describe, errors. The edge: gather, plan, pull, tick,
advise, the state file."""
import json
import os
import re
import threading
import time

from . import config, fleet, local

STATE = 'decisional.json'
NAME_RX = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,100}$')
GB_PER_B = 0.6        # a 4-bit model's weights per billion parameters (Ollama's default build)
OVERHEAD_GB = 2.0     # the context cache and buffers on top of the weights
VRAM_FILL = 0.95      # a model may take this much of the graphics memory
UNKNOWN_GB = 9999.0   # the need of a name that shows no size and has no `gb`: it fits nothing
RETRY_S = 6 * 3600    # after a failed pull, the next try waits this long
SKIP_TRANSPORTS = ('android', 'household')
_lock = threading.Lock()
_running = set()


# ---- settings and candidates (pure)

def settings(cfg):
    given = (cfg or {}).get('decisional')
    return {**config.DEFAULTS['decisional'], **(given if isinstance(given, dict) else {})}


def tag(name):
    return str(name) if ':' in str(name) else f'{name}:latest'


def same(a, b):
    """The same model name: `qwen3` is `qwen3:latest`."""
    return tag(a) == tag(b)


def entry_of(item):
    """{name, params_b, need_gb} of a candidates entry: a model name, or {model, gb} (gb: the memory it needs)."""
    name = str(item.get('model') or '') if isinstance(item, dict) else str(item or '')
    gb = item.get('gb') if isinstance(item, dict) else None
    p = fleet.params_b({'name': name})
    own = float(gb) if isinstance(gb, (int, float)) and not isinstance(gb, bool) and gb > 0 else None
    need = own if own is not None else (p * GB_PER_B + OVERHEAD_GB if p else UNKNOWN_GB)
    return {'name': name, 'params_b': p, 'need_gb': round(need, 1)}


def candidates(cfg):
    """The models to pick from, best first: `decisional.model` alone when it is set, else `decisional.candidates`."""
    s = settings(cfg)
    items = [s['model']] if s['model'] else (s['candidates'] if isinstance(s['candidates'], list) else [])
    return [e for e in (entry_of(i) for i in items) if NAME_RX.match(e['name'])]


# ---- hosts and the pick (pure)

def num(*values):
    """The first value that is a number, as a float; 0.0 when none is."""
    for v in values:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return 0.0


def host_row(name, machine, entry, bench, own):
    """{machine, vram_gb, mem_gb, models, ollama, stale, blocked} of one machine as a host (the hardware it was measured with,
    `decisional.hardware` winning; the models its Ollama has now)."""
    return {'machine': name, 'vram_gb': num(own.get('vram_gb'), bench.get('vram_gb')),
            'mem_gb': num(own.get('mem_gb'), bench.get('mem_gb')),
            'models': [m['name'] for m in (entry or {}).get('models') or [] if isinstance(m, dict) and m.get('name')],
            'ollama': bool(entry), 'stale': bool((entry or {}).get('stale')),
            'blocked': '' if config.is_on(machine) else 'paused by its owner'}


def hosts(cfg, fleet_state, bench):
    """Every machine that can hold a model, the strongest first: graphics memory, then memory, then name."""
    own = settings(cfg).get('hardware')
    own = own if isinstance(own, dict) else {}
    rows = [host_row(n, m, (fleet_state.get('machines') or {}).get(n), bench.get(n) or {}, own.get(n) or {})
            for n, m in (cfg.get('machines') or {}).items() if isinstance(m, dict) and m.get('transport') not in SKIP_TRANSPORTS]
    return sorted(rows, key=lambda h: (-h['vram_gb'], -h['mem_gb'], h['machine']))


def fit(c, h, s, installed):
    """'gpu' when the model fits the host's graphics memory, 'cpu' when allowed and it fits its memory with min_free_gb to spare,
    'unmeasured' for an installed model on a machine whose hardware is unknown, else ''."""
    if not (h['vram_gb'] or h['mem_gb']):
        return 'unmeasured' if installed else ''
    if c['need_gb'] <= h['vram_gb'] * VRAM_FILL:
        return 'gpu'
    return 'cpu' if s['allow_cpu'] and c['need_gb'] <= h['mem_gb'] - num(s['min_free_gb']) else ''


def has_model(models, name):
    return any(same(m, name) for m in models)


def placements(h, cands, s):
    """The candidates that fit the host, best first: {machine, model, placement, installed, need_gb}."""
    out = []
    for c in cands:
        installed = has_model(h['models'], c['name'])
        place = fit(c, h, s, installed)
        if place:
            out.append({'machine': h['machine'], 'model': c['name'], 'placement': place, 'installed': installed,
                        'need_gb': c['need_gb'], 'ollama': h['ollama']})
    return out


def why_none(h, cands):
    if not (h['vram_gb'] or h['mem_gb']):
        return 'its hardware is not measured yet (wait for the machine benchmark, or set decisional.hardware.<machine>.vram_gb)'
    least = min(c['need_gb'] for c in cands)
    return f'no candidate fits {h["vram_gb"]:g} GB of graphics memory (the smallest needs {least:g} GB; decisional.allow_cpu uses RAM)'


def eligible(hosts_, s):
    return [h for h in hosts_ if not s['host'] or h['machine'] == s['host']]


def host_note(hosts_, s):
    if not s['host'] or s['host'] in [h['machine'] for h in hosts_]:
        return []
    return [f'decisional.host names "{s["host"]}", which is not a machine that can hold a model']


def choose(cfg, hosts_):
    """{target, usable, skipped}: target is the best fit on the strongest suitable machine (what to pull), usable the best fit
    that is installed (what to ask now); skipped says why each other machine was passed over."""
    s, cands = settings(cfg), candidates(cfg)
    if not cands:
        return {'target': None, 'usable': None, 'skipped': ['decisional.candidates names no usable model: list free models, best first']}
    hits, skipped = [], []
    for h in eligible(hosts_, s):
        got = [] if h['blocked'] else placements(h, cands, s)
        hits += got
        skipped += [] if got else [f'{h["machine"]}: {h["blocked"] or why_none(h, cands)}']
    return {'target': hits[0] if hits else None, 'usable': next((x for x in hits if x['installed']), None),
            'skipped': skipped + host_note(hosts_, s)}


# ---- the plan (the edge: Tutti's own state files)

def gather(cfg, fleet_state=None, bench=None):
    from . import power
    st = fleet.read_state(cfg) if fleet_state is None else fleet_state
    return hosts(cfg, st, power.read(cfg) if bench is None else bench)


def plan(cfg, fleet_state=None, bench=None):
    """The pick now: choose over every machine's measured hardware and installed models (fleet.json, power.json)."""
    return choose(cfg, gather(cfg, fleet_state, bench))


def preferred(cfg, purpose, fleet_state=None, bench=None):
    """(machine, model) a purpose asks first, or None: decisional is on, the purpose is one of `decisional.purposes`, and the
    best fitting candidate is installed somewhere. tutti/brain.py then tries it before every other local model."""
    s = settings(cfg)
    if not s['enabled'] or purpose not in (s['purposes'] if isinstance(s['purposes'], list) else []):
        return None
    use = plan(cfg, fleet_state, bench)['usable']
    return (use['machine'], use['model']) if use else None


# ---- pulling a missing model

def state_path(cfg):
    return os.path.join(config.state_dir(cfg), STATE)


def read_state(cfg):
    try:
        with open(state_path(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(cfg, target, ok, why, now):
    tmp = f'{state_path(cfg)}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump({'pull': {'machine': target['machine'], 'model': target['model'], 'ok': ok, 'why': why, 'at': now}}, f)
    os.replace(tmp, state_path(cfg))


def worker_host(cfg, machine):
    """The model server address of the machine's local worker (a tunnel for another machine), or ''."""
    return next((cfg['workers'][n].get('host') or '' for n in fleet.local_names(cfg) if fleet.machine_of(cfg, n) == machine), '')


def install_fix(machine):
    return (f'no Ollama answered on {machine}: install it there once (https://ollama.com/download; on Linux '
            f'`curl -fsSL https://ollama.com/install.sh | sh`), let the dashboard probe it, then run `tutti decisional pull`')


def pull(cfg, target, post=None, now=None):
    """(ok, text): fetches the target's model on its machine through that machine's model server. Blocks until Ollama has it
    (up to decisional.pull_timeout_minutes). The outcome is kept for the retry wait."""
    now = time.time() if now is None else now
    if not target:
        return False, 'no machine fits a candidate: ' + '; '.join(plan(cfg)['skipped'])
    if target['installed']:
        return True, f'{target["model"]} is already on {target["machine"]}'
    host = worker_host(cfg, target['machine'])
    if not host:
        return False, install_fix(target['machine'])
    minutes = max(1.0, num(settings(cfg).get('pull_timeout_minutes')) or 120.0)
    try:
        r = (post or local.post)(host, {'model': target['model'], 'stream': False}, minutes * 60, path='/api/pull')
    except (OSError, ValueError) as e:
        return _outcome(cfg, target, False, f'{target["model"]} was not fetched on {target["machine"]} ({type(e).__name__}: {e}): '
                                            'check its model server and disk, then run `tutti decisional pull` again', now)
    ok = isinstance(r, dict) and r.get('status') == 'success'
    return _outcome(cfg, target, ok, f'{target["model"]} is now on {target["machine"]}' if ok else
                    f'{target["model"]} was not fetched on {target["machine"]}: Ollama answered {str(r)[:200]}', now)


def _outcome(cfg, target, ok, why, now):
    record(cfg, target, ok, why, now)
    return ok, why


def retry_wait(last, target, now):
    """A failed pull of the same model on the same machine is not tried again for RETRY_S."""
    last = last if isinstance(last, dict) else {}
    same_one = target and (last.get('machine'), last.get('model')) == (target['machine'], target['model'])
    return bool(same_one and not last.get('ok') and now - num(last.get('at')) < RETRY_S)


def pull_blocker(s, target, last, now):
    """Why the probe does not pull now, or '' when it should."""
    checks = ((not (s['enabled'] and s['auto_pull']), 'decisional.auto_pull is off'),
              (not target, 'no machine fits a candidate'),
              (bool(target and target['installed']), 'already installed'),
              (retry_wait(last, target, now), 'its last pull failed less than 6 hours ago'))
    return next((why for hit, why in checks if hit), '')


def person_here(cfg, target, quiet_fn=None):
    """A pull to this computer waits for a person to leave it (quiet mode)."""
    from . import quietmode
    machine = (cfg.get('machines') or {}).get(target['machine']) or {}
    return machine.get('transport') == 'local' and bool((quiet_fn or quietmode.status)(cfg)['quiet'])


def tick(cfg, now, start=None, pull_fn=None, quiet_fn=None):
    """The dashboard probe's round: with `decisional.auto_pull`, a missing target model is fetched in a background thread, one
    at a time. '' when a pull started, else why not."""
    s = settings(cfg)
    if not (s['enabled'] and s['auto_pull']) or (start is None and os.environ.get('TUTTI_NO_DAEMONS')):
        return 'decisional.auto_pull is off, or this is a check or a test'
    target = plan(cfg)['target']
    why = pull_blocker(s, target, read_state(cfg).get('pull'), now)
    why = why or ('a person is at this computer' if person_here(cfg, target, quiet_fn) else '')
    return why or launch(cfg, target, now, start, pull_fn)


def launch(cfg, target, now, start, pull_fn):
    key = (target['machine'], target['model'])
    with _lock:
        if _running:
            return 'a pull is already running'
        _running.add(key)
    (start or _start)(lambda: _job(cfg, target, pull_fn, now))
    return ''


def _start(fn):
    threading.Thread(target=fn, daemon=True).start()


def _job(cfg, target, pull_fn, now):
    try:
        (pull_fn or pull)(cfg, target, now=now)
    finally:
        with _lock:
            _running.discard((target['machine'], target['model']))


# ---- routing advice

def advice_prompt(report):
    return ('You are Tutti, a coding-task orchestrator. Below is each account\'s usage against its pace. In at most four short '
            'lines say where bounded coding work should go now: the accounts with room first (behind their pace, or about to '
            'reset), none to an account in panic or spent, and a local model when every account is ahead or closed. Name the '
            'accounts as written below and give no other advice.\n\n' + (report or '')[-4000:])


def advise(cfg, report, ask=None):
    """(advice, who) from the decisional model for the usage report; local only: no paid agent is asked for routing advice."""
    from . import brain
    return (ask or brain.ask)(cfg, advice_prompt(report), 'routing', online=False)


# ---- reading it

def describe(p, last=None):
    """`tutti decisional`: the pick, whether it is installed, the machines passed over and the last pull."""
    t, u = p['target'], p['usable']
    if not t:
        lines = ['decisional model: none fits']
    else:
        have = 'installed' if t['installed'] else ('missing: run `tutti decisional pull`' if t['ollama'] else install_fix(t['machine']))
        lines = [f'decisional model: {t["model"]} on {t["machine"]} ({t["placement"]}, needs about {t["need_gb"]:g} GB), {have}']
    if u and u is not t:
        lines.append(f'asked now: {u["model"]} on {u["machine"]} ({u["placement"]}) until the target is installed')
    lines += [f'  skipped {x}' for x in p['skipped']]
    if isinstance(last, dict) and last.get('why'):
        lines.append(f'last pull: {last["why"]}')
    return '\n'.join(lines)


# ---- validation

def bad_number(v):
    return isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0


def errors(cfg):
    s = (cfg or {}).get('decisional')
    if s is None:
        return []
    if not isinstance(s, dict):
        return [f'decisional is {s!r}: a mapping such as {{enabled: true, auto_pull: false}} (docs/FLEET.md)']
    errs = _name_errors(cfg, s) + _hardware_errors(cfg, s.get('hardware'))
    errs += [f'decisional.{k} is {s[k]!r}: use a number, 0 or more' for k in ('min_free_gb', 'pull_timeout_minutes')
             if k in s and bad_number(s[k])]
    return errs + [f'decisional.{k} is {s[k]!r}: use true or false' for k in ('enabled', 'auto_pull', 'allow_cpu')
                   if k in s and not isinstance(s[k], bool)]


def _name_errors(cfg, s):
    errs = []
    if s.get('host') and s['host'] not in (cfg.get('machines') or {}):
        errs.append(f'decisional.host names "{s["host"]}", which is not under machines: name one of them, or leave it empty')
    if s.get('model') and not NAME_RX.match(str(s['model'])):
        errs.append(f'decisional.model is {s["model"]!r}: use an Ollama model name such as qwen3:32b, or leave it empty')
    errs += [f'decisional.{k} is {s[k]!r}: use a list' for k in ('candidates', 'purposes') if k in s and not isinstance(s[k], list)]
    return errs + _candidate_errors(s.get('candidates'))


def _candidate_errors(items):
    errs = []
    for item in items if isinstance(items, list) else []:
        e = entry_of(item)
        if not NAME_RX.match(e['name']) or e['need_gb'] >= UNKNOWN_GB:
            errs.append(f'decisional.candidates has {item!r}: use an Ollama model name that shows its size (qwen3:32b), '
                        'or {model: name, gb: memory it needs}')
    return errs


def _hardware_errors(cfg, hw):
    if hw in (None, {}):
        return []
    if not isinstance(hw, dict):
        return [f'decisional.hardware is {hw!r}: a mapping {{<machine>: {{vram_gb: 32, mem_gb: 64}}}}']
    errs = [f'decisional.hardware.{n} is not under machines: name a machine' for n in hw if n not in (cfg.get('machines') or {})]
    return errs + [e for n, v in hw.items() for e in _gb_errors(n, v)]


def _gb_errors(name, v):
    if not isinstance(v, dict):
        return [f'decisional.hardware.{name} is {v!r}: use {{vram_gb: <GB>, mem_gb: <GB>}}']
    return [f'decisional.hardware.{name}.{k} is {x!r}: use vram_gb or mem_gb, a number above 0'
            for k, x in v.items() if k not in ('vram_gb', 'mem_gb') or num(x) <= 0]
