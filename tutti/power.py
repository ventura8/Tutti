"""How strong each machine is, measured, and which local model a task deserves (docs/DISPATCH.md, docs/FLEET.md).

- **Machine power, measured not guessed.** A short benchmark (about two seconds: one core hashing 256 MB, a 64 MB synced disk
  write, the core and thread count, total memory, the GPU's memory, an NPU) runs once per machine and again every
  `power.bench_days` (7). `score` turns it into one number (CPU + memory + GPU + disk points); `headroom` scales it by what is
  free right now (load per core, free memory, heat, Tutti's share: quiet, presence and limited hours already lower it);
  `effective` is the two together. Dispatch sends heavy shards (e2e, builds, full suites, long ones) to the strongest free
  machine first and light ones to the weaker machines; a weak machine short of headroom takes no heavy shard at all.
- **Model tiers by measured capability.** Every household model gets a tier 0 (light), 1 (mid) or 2 (strong): its class by
  size and quantisation (fleet.TIERS), moved up or down by its pass rate on Tutti's own task history (at least 4 attempts),
  and its speed is shown beside it. A task's difficulty (0-2) is its size (S, M, L) plus one per failed attempt: an easy task
  goes to the weakest model that can do it, keeping the strong ones free; after a failure it escalates a tier.

Pure: parse_bench, score, headroom, effective, heavy, weak, difficulty, model_tier, prefer, why_text. The edge: measure,
refresh (a background thread per stale machine), the state file."""
import json
import os
import re
import subprocess
import threading
import time

from . import config

FILE = 'power.json'
BENCH_DAYS = 7
HEAVY_KINDS = ('e2e', 'build', 'full', 'docker', 'integration')
HEAVY_MINUTES = 15
WEAK_SHARE = 0.5      # a machine under half the strongest free machine's effective power is weak
LOW_HEADROOM = 0.3    # under this a weak machine takes no heavy shard at all
SIZE_LEVEL = {'S': 0, 'M': 1, 'L': 2}
TIER_NAMES = ('light', 'mid', 'strong')
MIN_RUNS = 4
TOKEN_SPLIT = re.compile(r'[/_\-:\s]+')
# One line, `B <cores> <threads> <mem GB> <cpu MB/s> <disk MB/s> <VRAM GB> <npu 0|1>`; read-only but a temp file it removes
BENCH_SCRIPT = (
    'ns(){ date +%s%N; }; c=$(grep -c ^processor /proc/cpuinfo 2>/dev/null || nproc); '
    'p=$(lscpu -p=core 2>/dev/null | grep -v "^#" | sort -u | wc -l); [ "${p:-0}" -gt 0 ] || p=$c; '
    'm=$(awk \'/MemTotal/{printf "%.1f", $2/1048576}\' /proc/meminfo 2>/dev/null); '
    's=$(ns); head -c 268435456 /dev/zero | sha256sum > /dev/null; e=$(ns); cpu=$((256000000000 / ((e - s) + 1))); '
    'f=$(mktemp "${TMPDIR:-/tmp}/tutti-bench.XXXXXX"); s=$(ns); dd if=/dev/zero of="$f" bs=1M count=64 conv=fdatasync 2>/dev/null; '
    'e=$(ns); rm -f "$f"; disk=$((64000000000 / ((e - s) + 1))); '
    'v=$( { nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null || '
    '/usr/lib/wsl/lib/nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null; } '
    '| grep -E "^[0-9]+$" | sort -n | tail -n 1); '
    'n=0; ls /dev/accel/accel* > /dev/null 2>&1 && n=1; '
    'echo "B $p $c ${m:-0} $cpu $disk $(( ${v:-0} / 1024 )) $n"')
_lock = threading.Lock()
_running = set()


# ---- machine power (pure)

def parse_bench(text):
    """{cores, threads, mem_gb, cpu_mbs, disk_mbs, vram_gb, npu} from the benchmark's `B ...` line, or None."""
    for line in (text or '').splitlines():
        parts = line.split()
        if len(parts) == 8 and parts[0] == 'B':
            try:
                c, t, m, cpu, disk, v, n = (float(x) for x in parts[1:])
            except ValueError:
                return None
            return {'cores': int(c), 'threads': int(t), 'mem_gb': m, 'cpu_mbs': cpu, 'disk_mbs': disk, 'vram_gb': v, 'npu': n > 0}
    return None


def parts(b):
    """The points behind a score: CPU (one core's speed times the cores, hyper-threads at a third), memory, GPU, disk."""
    cores = max(int(b.get('cores') or 1), 1)
    threads = max(int(b.get('threads') or cores), cores)
    cpu = float(b.get('cpu_mbs') or 0) / 100 * (cores + (threads - cores) / 3)
    return {'cpu': round(cpu), 'memory': round(float(b.get('mem_gb') or 0) * 2), 'gpu': round(float(b.get('vram_gb') or 0) * 8
                                                                                              + (20 if b.get('npu') else 0)),
            'disk': round(min(float(b.get('disk_mbs') or 0), 3000) / 30)}


def score(b):
    """One comparable number for a benchmark (0 when there is none yet)."""
    return sum(parts(b).values()) if b else 0


def headroom(row):
    """0..1: how much of the machine is free now: load per core, free memory (under 4 GB counts down to 0 at 1 GB), heat
    (at or over its cap counts half) and Tutti's share (use_pct: quiet, presence, limited hours). An unreachable or
    blocked machine has none."""
    if not row or not row.get('reachable', True) or row.get('status') in ('offline', 'off', 'held', 'paused'):
        return 0.0
    cores = max(float(row.get('cores') or 1), 1.0)
    h = max(0.0, 1 - float(row.get('load') or 0) / cores)
    mem = row.get('mem_free_gb')
    if mem is not None:
        h *= max(0.0, min(1.0, (float(mem) - 1) / 3))
    cap = (row.get('heat') or {}).get('cap_c')
    hot = max([t for t in (row.get('temp_c'), row.get('gpu_temp_c')) if t is not None] or [0])
    if cap is not None and hot >= cap:
        h *= 0.5
    pct = row.get('use_pct')
    return round(h * (100 if pct is None else float(pct)) / 100, 2)


def effective(power, room):
    return round(power * room)


def heavy(w):
    """A heavy shard: kind or key token e2e, build, full, docker or integration, or minutes of HEAVY_MINUTES or more."""
    text = f'{w.get("kind") or ""} {w.get("key") or ""}'.lower()
    tokens = set(TOKEN_SPLIT.split(text))
    return bool(tokens & set(HEAVY_KINDS)) or float(w.get('minutes') or 0) >= HEAVY_MINUTES


def weak(t, best):
    """A target is weak when its effective power (measured power times headroom now) is under WEAK_SHARE of the strongest
    usable machine's measured power (devices: never weak here, their own policy decides; an unmeasured machine has score 0
    and is treated as weak once any machine is measured)."""
    if t.get('kind') != 'machine' or not best:
        return False
    return float(t.get('effective') or 0) < WEAK_SHARE * best


def suited(t, ready, best):
    """(the shards this target should take, why it leaves the others): a strong machine takes heavy shards first, a weak one
    light ones first; a weak machine short of headroom takes no heavy shard at all."""
    if t.get('kind') != 'machine' or not ready:
        return ready, ''
    hv = [w for w in ready if heavy(w)]
    lt = [w for w in ready if not heavy(w)]
    if not weak(t, best):
        return (hv or lt), ''
    if lt:
        return lt, ''
    if float(t.get('room', 1)) < LOW_HEADROOM:
        return [], (f'{t["name"]} is a weaker machine with little room now (power {t.get("power", 0)}, '
                    f'{round(float(t.get("room", 0)) * 100)} % free): heavy shards wait for a stronger one')
    return hv, ''


def pick_why(t, w, best):
    """Why this target took this shard, for the dashboard."""
    load = 'heavy' if heavy(w) else 'light'
    if t.get('kind') != 'machine':
        return f'{load} shard on device {t["name"]}'
    rank = 'the strongest machine' if not best or float(t.get('power') or 0) >= best else \
        'a weaker machine' if weak(t, best) else 'a strong machine'
    return (f'{load} shard on {t["name"]}: {rank} (power {t.get("power", 0)}, {round(float(t.get("room", 1)) * 100)} % free'
            f', effective {t.get("effective", 0)})')


# ---- model tiers (pure)

def difficulty(task):
    """0 (easy) .. 2 (hard): the size (S 0, M 1, L 2), one up per failed attempt (escalation), a lint-fix or review at least 1."""
    from . import fleet
    d = SIZE_LEVEL.get(task.get('size') or 'S', 0)
    if fleet.kind_of(task) in ('lint-fix', 'review'):
        d = max(d, 1)
    d += sum(1 for a in task.get('attempts') or [] if isinstance(a, dict) and not a.get('ok'))
    return min(d, 2)


def pass_rates(tasks):
    """{model: (passed, attempts)} over Tutti's task history (attempts that name their model)."""
    out = {}
    for t in tasks or []:
        for a in t.get('attempts') or []:
            if isinstance(a, dict) and a.get('model'):
                p, n = out.get(a['model'], (0, 0))
                out[a['model']] = (p + bool(a.get('ok')), n + 1)
    return out


def model_tier(m, rates=None):
    """(tier 0..2, why): the size class (fleet.TIERS rank: small 0, normal 1, strong 2; a 2-3 bit (under 4 bits) quantisation
    counts one down), then its measured pass rate: 75 % or more over MIN_RUNS attempts up one, 25 % or less down one."""
    from . import brain, fleet
    tier = fleet.TIERS[m.get('tier') or 'normal']['rank']
    why = [f'{m.get("params_b") or "?"}B {m.get("tier") or "normal"}']
    if tier and brain.quant_factor(m.get('quant')) < 0.9:
        tier, why = tier - 1, [*why, f'{m.get("quant")} quantisation']
    p, n = (rates or {}).get(m.get('name'), (0, 0))
    if n >= MIN_RUNS:
        rate = p / n
        step = 1 if rate >= 0.75 else -1 if rate <= 0.25 else 0
        tier = max(0, min(2, tier + step))
        why.append(f'passed {p} of {n}')
    return tier, ', '.join(why)


_RATES = {}


def rates(cfg, ttl=60, clock=time.monotonic):
    """pass_rates over the autopilot's task history, read at most once a minute."""
    from . import autopilot
    key = config.state_dir(cfg)
    at, value = _RATES.get(key, (None, None))
    if at is None or clock() - at >= ttl:
        value = pass_rates(autopilot.read_state(cfg).get('tasks') or [])
        _RATES[key] = (clock(), value)
    return value


def prefer(tier, need):
    """Sort key: the weakest tier that meets the difficulty first (the strong ones stay free); a tier under it last."""
    return tier - need if tier >= need else 10 + need - tier


# ---- the edge: the state file and the benchmark

def path(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def read(cfg):
    try:
        with open(path(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_one(cfg, name, bench, now):
    with _lock:
        data = read(cfg)
        data[name] = {**bench, 'at': now}
        tmp = f'{path(cfg)}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf8') as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path(cfg))


def measure(machine, run=subprocess.run, timeout=60):
    """The benchmark on the machine (here, or over its ssh): a bench dict or None."""
    from . import remote
    if machine.get('transport') == 'local':
        argv = ['bash', '-c', BENCH_SCRIPT]
    elif machine.get('transport') == 'ssh' and machine.get('host'):
        argv = [*remote.SSH, *remote.QUICK, machine['host'], BENCH_SCRIPT]
    else:
        return None
    try:
        r = run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_bench(r.stdout)


def days(cfg):
    return float((cfg.get('power') or {}).get('bench_days') or BENCH_DAYS)


def stale(entry, now, every_days):
    return not entry or now - float(entry.get('at') or 0) >= every_days * 86400


def refresh(cfg, rows, now, measure_fn=measure, start=None):
    """Each reachable machine whose benchmark is missing or older than bench_days is measured in a background thread (one at
    a time per machine; a busy machine waits until it is free so the benchmark neither slows nor misreads its work).
    Returns the names started."""
    data, started = read(cfg), []
    if start is None and os.environ.get('TUTTI_NO_DAEMONS'):
        return started   # a check or a test: no real benchmark outlives it (tutti/procs.py)
    for r in rows or []:
        name = r.get('name')
        m = (cfg.get('machines') or {}).get(name)
        if not m or not r.get('reachable') or r.get('status') != 'free' or not stale(data.get(name), now, days(cfg)):
            continue
        with _lock:
            if name in _running:
                continue
            _running.add(name)
        (start or _start)(lambda n=name, mm=m: _bench(cfg, n, mm, now, measure_fn))
        started.append(name)
    return started


def _start(fn):
    threading.Thread(target=fn, daemon=True).start()


def _bench(cfg, name, m, now, measure_fn):
    try:
        b = measure_fn({**m, 'name': name})
        if b:
            save_one(cfg, name, b, now)
    finally:
        with _lock:
            _running.discard(name)


def annotate(cfg, rows):
    """Every machine row gets `power`: {score, parts, room, effective, bench (the measurements), at} (score 0 until measured)."""
    data = read(cfg)
    for r in rows or []:
        b = data.get(r.get('name'))
        s = score(b)
        room = headroom(r)
        r['power'] = {'score': s, 'parts': parts(b) if b else {}, 'room': room, 'effective': effective(s, room),
                      'bench': {k: b.get(k) for k in ('cores', 'threads', 'mem_gb', 'cpu_mbs', 'disk_mbs', 'vram_gb', 'npu')}
                      if b else None, 'at': (b or {}).get('at')}
    return rows


def after_probe(cfg, rows, now, measure_fn=measure, start=None):
    """The dashboard's probe round: stale benchmarks started, every row annotated."""
    refresh(cfg, rows, now, measure_fn, start)
    return annotate(cfg, rows)
