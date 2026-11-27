"""Quiet mode: while a person is at this computer (keyboard or mouse used in the last `in_use_minutes`, screen unlocked) or
its free memory is under the user's reserve, Tutti and the tools that ask it keep the computer for that person
(docs/ARCHITECTURE.md, *Quiet mode*).

- `status(cfg)`: {quiet, reasons, idle_s, avail_gb}; `tutti quiet status` exits 0 while quiet, 1 when free, so any script
  (ship.sh, nuc-test.sh, check.sh runners, a Claude agent) can ask before it starts heavy work here.
- `guard_round(cfg)`: one round of the guard the dashboard runs every probe: Tutti's own running jobs here (the `tutti quiet`
  commands, by the exact pid in their `.pid` file, and their process trees) get nice 19 and idle I/O while a person is here;
  under the memory reserve they are stopped (SIGSTOP) and continued (SIGCONT) once memory is back or the person left; the
  local Ollama models are unloaded. Nothing is ever matched by name: only pids Tutti started.
- `wrap(argv, cfg)`: the command `tutti quiet` runs, niced and CPU-capped (a systemd-run --user scope) while quiet."""
import json
import os
import signal
import subprocess
import time
import urllib.request

from . import config, local, share

RESERVE_GB = 4.0     # free memory kept for the person (quiet.reserve_gb)
CPU_QUOTA = '200%'   # the CPU a command started while quiet may use (quiet.cpu_quota): two cores


def settings(cfg):
    q = (cfg or {}).get('quiet') or {}
    local = next((m for m in ((cfg or {}).get('machines') or {}).values() if m.get('transport') == 'local'), {})
    return {'reserve_gb': float(q.get('reserve_gb', RESERVE_GB)), 'cpu_quota': str(q.get('cpu_quota', CPU_QUOTA)),
            'in_use_minutes': float(share.setting(local, 'in_use_minutes'))}


def avail_gb(path='/proc/meminfo'):
    try:
        with open(path, encoding='utf8') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) / 1048576
    except OSError:
        pass
    return None


def _ha_present(cfg):
    """True when Home Assistant reports a person home (presence_entities); False when unconfigured or unreachable."""
    from . import homeassistant
    return homeassistant.is_person_present(cfg)


def status(cfg, idle_fn=None, mem_fn=None):
    s = settings(cfg)
    idle = (idle_fn or share.idle_seconds)()
    mem = (mem_fn or avail_gb)()
    ha_present = _ha_present(cfg)
    reasons = []
    at_computer = idle is not None and idle < s['in_use_minutes'] * 60
    if at_computer:
        reasons.append(f'a person is at this computer (last input {int(idle)} s ago)')
    if ha_present:
        reasons.append('a person is present (Home Assistant)')
    if mem is not None and mem < s['reserve_gb']:
        reasons.append(f'free memory {mem:.1f} GB is under the {s["reserve_gb"]:g} GB kept for the person')
    return {'quiet': bool(reasons), 'person': at_computer or ha_present,
            'low_memory': mem is not None and mem < s['reserve_gb'], 'reasons': reasons, 'idle_s': idle, 'avail_gb': mem}


def quiet_dir(cfg):
    return os.path.join(config.state_dir(cfg), 'quiet')


def own_pids(cfg, proc='/proc'):
    """The pids of Tutti's own running `tutti quiet` jobs here (their `<log>.pid` files) with every descendant."""
    roots = set()
    d = quiet_dir(cfg)
    for name in os.listdir(d) if os.path.isdir(d) else []:
        if name.endswith('.pid') and not os.path.exists(os.path.join(d, name[:-4] + '.rc')):
            try:
                with open(os.path.join(d, name), encoding='utf8') as f:
                    roots.add(int(f.read().strip()))
            except (OSError, ValueError):
                continue
    return descendants(roots, proc)


def descendants(roots, proc='/proc'):
    kids = {}
    for p in os.listdir(proc):
        if not p.isdigit():
            continue
        try:
            with open(os.path.join(proc, p, 'stat'), encoding='utf8', errors='replace') as f:
                ppid = int(f.read().rsplit(')', 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        kids.setdefault(ppid, []).append(int(p))
    out, todo = set(), [r for r in roots if os.path.isdir(os.path.join(proc, str(r)))]
    while todo:
        pid = todo.pop()
        if pid not in out:
            out.add(pid)
            todo.extend(kids.get(pid, []))
    return out


def stopped_path(cfg):
    return os.path.join(quiet_dir(cfg), 'stopped.json')


def _load(path):
    try:
        with open(path, encoding='utf8') as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def _save(path, pids):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf8') as f:
        json.dump(sorted(pids), f)


def calm(pids, run=subprocess.run):
    """nice 19 and idle I/O for these exact pids."""
    for pid in pids:
        try:
            os.setpriority(os.PRIO_PROCESS, pid, 19)
        except OSError:
            continue
        run(['ionice', '-c', '3', '-p', str(pid)], capture_output=True, check=False)


def signal_all(pids, sig, kill=os.kill):
    done = set()
    for pid in pids:
        try:
            kill(pid, sig)
            done.add(pid)
        except OSError:
            continue
    return done


def unload_ollama(cfg, post=None):
    """Asks this computer's Ollama to unload every loaded model (keep_alive 0); the names it unloaded."""
    host = ((cfg or {}).get('quiet') or {}).get('ollama', 'http://127.0.0.1:11434')
    try:
        with urllib.request.urlopen(f'{host}/api/ps', timeout=3) as r:
            models = [m['name'] for m in json.load(r).get('models', [])]
    except (OSError, ValueError, KeyError):
        return []
    for name in models:
        try:   # an unload, not a model call: local.post with keep_alive 0 (as local.wait_for_yield does)
            (post or local.post)(host, {'model': name, 'keep_alive': 0}, 30)
        except OSError:
            continue
    return models


def guard_round(cfg, st=None, pids=None, kill=os.kill, run=subprocess.run, unload=None):
    """One round: calm Tutti's own jobs while quiet, stop them under the memory reserve, continue them after."""
    st = st or status(cfg)
    pids = own_pids(cfg) if pids is None else pids
    path = stopped_path(cfg)
    stopped = _load(path) & pids   # only pids still in Tutti's own jobs: never a reused pid
    acts = []
    if st['quiet'] and pids:
        calm(pids, run)
        acts.append(f'calmed {len(pids)} pid(s)')
    if st['low_memory'] and st['person']:
        new = signal_all(pids - stopped, signal.SIGSTOP, kill)
        stopped |= new
        if new:
            acts.append(f'stopped {sorted(new)}')
    elif stopped:
        signal_all(stopped, signal.SIGCONT, kill)
        acts.append(f'continued {sorted(stopped)}')
        stopped = set()
    if st['person']:
        gone = (unload or unload_ollama)(cfg)
        if gone:
            acts.append(f'unloaded {", ".join(gone)}')
    _save(path, stopped)
    return acts


def wrap(argv, cfg, st=None, which=None):
    """argv, run at nice 19 with idle I/O and a CPU quota while quiet; unchanged when the computer is free."""
    import shutil
    st = st or status(cfg)
    if not st['quiet']:
        return list(argv)
    which = which or shutil.which
    out = ['nice', '-n', '19']
    if which('ionice'):
        out += ['ionice', '-c', '3']
    if which('systemd-run'):
        out = ['systemd-run', '--user', '--scope', '--quiet', '-p', f'CPUQuota={settings(cfg)["cpu_quota"]}'] + out
    return out + list(argv)


def wait_free(cfg, max_s=None, poll=30, sleep=time.sleep, clock=time.monotonic, status_fn=None):
    """Waits until this computer is free; False when `max_s` passed first."""
    start = clock()
    while (status_fn or status)(cfg)['quiet']:
        if max_s is not None and clock() - start >= max_s:
            return False
        sleep(poll)
    return True
