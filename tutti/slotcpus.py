"""A slot's CPU budget (docs/FLEET.md, *Slot CPU budget*): `tutti test` gives every command it runs `TUTTI_SLOT_CPUS`, the
machine's CPUs shared among its slots, and sets the worker knobs of the common test tools to it (Playwright, Vitest, Jest,
pytest-xdist, CMake, make, Go, OpenMP) unless the caller set them. Two runs on a 28-CPU machine used to size Playwright and
Vitest from all 28 visible CPUs each; with emulators alongside, triggers ran minutes late and specs failed (2026-11-25).

Pure: budget, count_expr, export_line, docker_args, errors. The CPU count is read on the machine (`nproc`) unless
`machines.<m>.cores` names it, so the line is the same for any machine."""
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
THERMAL = 'cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | sort -n | tail -n 1'


def budget(cpus, slots, pct=100, override=None):
    """CPUs one slot may use: `override` (machines.<m>.slot_cpus) as is, else floor(cpus * pct / 100 / slots), at least 1."""
    if override is not None:
        return max(1, int(override))
    return max(1, int(cpus * pct / 100 / max(1, int(slots))))


def enabled(cfg):
    return bool(((cfg or {}).get('test') or {}).get('slot_cpu_env', True))


def count_expr(machine, now=None):
    """Bash that prints the budget, evaluated on the machine when the run is queued: its CPUs (`cores` or nproc), at most the
    docker runner's `--cpus`, at most heat.throttle_cpus when it is at its heat cap, at the share of a limited-hours window
    (`now`, a UTC datetime), divided among the slots (the same arithmetic as budget())."""
    override = machine.get('slot_cpus')
    if override is not None:
        return str(budget(0, 1, override=override))
    pct = share.limited(machine, now) if now is not None else None
    pct = 100 if pct is None else max(1, int(pct))
    r = machine.get('runner') or {}
    parts = [f'n={int(machine["cores"])}' if machine.get('cores') else 'n=$(nproc 2>/dev/null || echo 1)']
    if r.get('kind') == 'docker' and r.get('cpus'):
        cap = max(1, int(float(r['cpus'])))
        parts.append(f'[ "$n" -le {cap} ] || n={cap}')
    heat = machine.get('heat') or {}
    if heat.get('cap_c') is not None:
        low = max(1, int(float(heat.get('throttle_cpus') or 1)))
        parts.append(f't=$({THERMAL}); [ "${{t:-0}}" -lt {int(float(heat["cap_c"]) * 1000)} ] || [ "$n" -le {low} ] || n={low}')
    parts.append(f'n=$((n * {pct} / 100 / {max(1, int(machine.get("slots") or 1))})); [ "$n" -ge 1 ] || n=1; echo "$n"')
    return '$(' + '; '.join(parts) + ')'


def export_line(machine, now=None):
    """`export ... && ` for the front of a run's script: the budget and each knob the caller has not set (`${X:-...}` keeps a
    value already in the environment; only unset or empty names are filled)."""
    sets = [f'{NAME}="${{{NAME}:-{count_expr(machine, now)}}}"']
    sets += [f'{k}="${{{k}:-{v.format(n=f"${NAME}")}}}"' for k, v in KNOBS.items()]
    return ' && '.join(f'export {x}' for x in sets) + ' && '   # one export each: a later one reads the earlier


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
