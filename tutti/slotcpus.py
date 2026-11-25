"""A slot's CPU budget (docs/FLEET.md, *Slot CPU budget*): `tutti test` gives every command it runs `TUTTI_SLOT_CPUS`, the
machine's CPUs shared among its slots, and sets the worker knobs of the common test tools to it (Playwright, Vitest, Jest,
pytest-xdist, CMake, make, Go, OpenMP) unless the caller set them. Two runs on a 28-CPU machine used to size Playwright and
Vitest from all 28 visible CPUs each; with emulators alongside, triggers ran minutes late and specs failed (2026-11-25).

Pure: budget, slot_env, docker_args, errors. The number itself is computed by scripts/slot.sh once it holds its lock (heat and
limited hours as they are when the run starts, not when it was queued); the CPU count is read there (`nproc`) unless
`machines.<m>.cores` names it, so the inputs are the same for any machine."""
import shlex

from . import share

NAME = 'TUTTI_SLOT_CPUS'
# name -> value template ({n} is the budget); set only when the caller has not set the name already
KNOBS = {
    'PLAYWRIGHT_WORKERS': '{n}',
    'VITEST_MAX_THREADS': '{n}',
    'VITEST_MIN_THREADS': '1',
    'VITEST_MAX_FORKS': '{n}',
    'JEST_WORKERS': '{n}',
    'PYTEST_XDIST_AUTO_NUM_WORKERS': '{n}',
    'CMAKE_BUILD_PARALLEL_LEVEL': '{n}',
    'MAKEFLAGS': '-j{n}',
    'GOMAXPROCS': '{n}',
    'OMP_NUM_THREADS': '{n}',
}


def budget(cpus, slots, pct=100, override=None):
    """CPUs one slot may use: `override` (machines.<m>.slot_cpus) as is, else floor(cpus * pct / 100 / slots), at least 1."""
    if override is not None:
        return max(1, int(override))
    return max(1, int(cpus * pct / 100 / max(1, int(slots))))


def enabled(cfg):
    return bool(((cfg or {}).get('test') or {}).get('slot_cpu_env', True))


def slot_env(machine, zone='UTC'):
    """The inputs scripts/slot.sh needs to compute the budget itself, as `NAME=value` words: it reads the heat probe and the
    clock after it has taken its lock, so a long wait in the queue never uses stale numbers. `cores` (else nproc there),
    `slots`, `slot_cpus`, the docker runner's `--cpus`, heat.cap_c with throttle_cpus, the limited window with its pct (hours
    read in `zone`), and the knobs. Heat and window are shell-quoted here and parsed there the way share.limited() does."""
    r = machine.get('runner') or {}
    env = {'SLOT_CPU_SLOTS': max(1, int(machine.get('slots') or 1)), 'SLOT_CPU_KNOBS': ' '.join(f'{k}={v}' for k, v in KNOBS.items())}
    if machine.get('slot_cpus') is not None:
        env['SLOT_CPU_OVERRIDE'] = budget(0, 1, override=machine['slot_cpus'])
    if machine.get('cores'):
        env['SLOT_CPU_CORES'] = int(machine['cores'])
    if r.get('kind') == 'docker' and r.get('cpus'):
        env['SLOT_CPU_DOCKER'] = max(1, int(float(r['cpus'])))
    heat = machine.get('heat') or {}
    if heat.get('cap_c') is not None:
        env['SLOT_CPU_THROTTLE'] = max(1, int(float(heat.get('throttle_cpus') or 1)))
    lim = machine.get('limit')
    if isinstance(lim, dict) and share.WINDOW.fullmatch(str(lim.get('window', ''))):
        env['SLOT_CPU_WINDOW'] = lim['window']
        env['SLOT_CPU_DAYS'] = ','.join(lim.get('days') or share.DAYS)
        env['SLOT_CPU_PCT'] = max(1, int(lim.get('pct', share.LIMIT_PCT)))
        env['SLOT_CPU_ZONE'] = zone
    return ' '.join(f'{k}={shlex.quote(str(v))}' for k, v in env.items())


def docker_args():
    """Docker arguments that hand the budget and the knobs, exported before docker starts, into the container."""
    return ' '.join(f'-e {shlex.quote(k)}' for k in (NAME, *KNOBS))


def errors(name, machine):
    v = machine.get('slot_cpus')
    if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
        return [f'machines.{name}.slot_cpus is {v!r}: a whole number of CPUs per slot, 1 or more, for example 4 (or remove it: '
                'the machine\'s CPUs divided by its slots)']
    c = machine.get('cores')
    if c is not None and (isinstance(c, bool) or not isinstance(c, int) or c < 1):
        return [f'machines.{name}.cores is {c!r}: a whole number of CPUs, 1 or more (or remove it: read with nproc)']
    return []
