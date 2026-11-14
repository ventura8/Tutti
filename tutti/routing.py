"""Fall-over routing: which machine runs a heavy command. Pure logic; the probes come from the caller."""
import re
from dataclasses import dataclass, field

from . import controls, devices


@dataclass
class Probe:
    """What one look at a machine found. free: slots nobody holds; yielding: its yield flag is up."""
    reachable: bool = False
    free: int = 0
    load: float = 0.0
    cores: int = 1
    yielding: bool = False
    temp: float | None = None     # the CPU: the hottest thermal zone in degrees C (a WSL machine: Windows' own reading), or None
    containers: list = field(default_factory=list)   # `<name> <running for>` of its running test containers
    gpu_temp: float | None = None  # the hottest GPU in degrees C (nvidia-smi, or an amdgpu/xe/i915 sensor), None when none
    temp_note: str = ''            # why the CPU temperature is not readable (a WSL machine whose Windows side has no sensor)
    readings: dict | None = None   # a Termux machine's battery, charging, battery_c and wifi (tutti/devices.py), else None

    def hottest(self):
        """(degrees C, 'CPU' or 'GPU') of the hotter of the two, or (None, '') when neither is readable."""
        found = [(t, w) for t, w in ((self.temp, 'CPU'), (self.gpu_temp, 'GPU')) if t is not None]
        return max(found) if found else (None, '')


class NoMachine(ValueError):
    """Every machine is paused, off or refuses runs by its power policy: nothing may be queued anywhere."""


@dataclass
class Choice:
    machine: str
    queued: bool
    reason: str


def parse_temp(word):
    """Millidegrees from /sys/class/thermal (`96050`), or 0 / missing when nothing is readable: degrees C or None."""
    try:
        t = float(word) / 1000
    except (TypeError, ValueError):
        return None
    return t if t > 0 else None


def parse_gpu_temp(word):
    """Degrees C of the hottest GPU (`43` or `43.20` from nvidia-smi), or None for `-`, missing or nonsense."""
    try:
        t = float(word)
    except (TypeError, ValueError):
        return None
    return t if 0 < t < 150 else None


def parse_probe(text):
    """The probe script prints `<free> <load> <cores> <yield 0|1> [<millidegrees> [<GPU degrees or ->]]`; anything else means
    the machine did not answer. The temperatures are optional: an older probe (or a machine without sensors) leaves them out."""
    parts = (text or '').split()
    try:
        temp = parse_temp(parts[4] if len(parts) > 4 else None)
        gpu = parse_gpu_temp(parts[5] if len(parts) > 5 else None)
        return Probe(True, int(parts[0]), float(parts[1]), max(1, int(parts[2])), parts[3] == '1', temp, gpu_temp=gpu)
    except (IndexError, ValueError):
        return Probe()


def heat_unfit(machine, probe):
    """Why the machine's temperature refuses a run, or ''. `heat.stop_c`: nothing new at or above it; `heat.cap_c`: at or above
    it only `heat.hot_slots` (default 1) runs at a time, so a hot machine cools down instead of throttling every run."""
    heat = machine.get('heat') or {}
    t, which = probe.hottest()
    if t is None or not heat:
        return ''
    if heat.get('stop_c') is not None and t >= heat['stop_c']:
        return f'too hot: {which} {t:.0f} C (nothing new at {heat["stop_c"]:g} C or more)'
    busy = int(machine.get('slots', 1)) - probe.free
    hot_slots = int(heat.get('hot_slots', 1))
    if heat.get('cap_c') is not None and t >= heat['cap_c'] and busy >= hot_slots:
        return f'hot: {which} {t:.0f} C, slots capped to {hot_slots} at {heat["cap_c"]:g} C or more ({busy} running)'
    return ''


def power_unfit(machine, probe):
    """Why a Termux machine's battery, charging, heat or Wi-Fi refuses a run (its policy), or ''. Other machines: ''."""
    if machine.get('transport') != 'android':
        return ''
    return devices.unfit(devices.policy(machine, termux=True), probe.readings or {})


def owner_unfit(machine):
    """Paused by the owner (dashboard or `tutti pause`), or an Android machine whose lint and unit runs are not opted in."""
    if machine.get('paused'):
        return controls.paused_note(machine)
    if machine.get('transport') == 'android' and machine.get('enabled') is not True:
        return 'off: Android lint and unit runs are opt-in; turn them on from the dashboard or with `tutti optin machine <name> on`'
    return ''


def unfit(machine, probe):
    """Why this machine cannot take a run right now, or '' when it can."""
    owner = owner_unfit(machine)
    if owner:
        return owner
    if not probe.reachable:
        return 'unreachable'
    if probe.yielding:
        return 'yield flag is up'
    if probe.free < 1:
        return 'no free slot'
    ratio = machine.get('max_load_ratio')
    if ratio is not None and probe.load >= ratio * probe.cores:
        return f'load {probe.load:g} is over {ratio:g} x {probe.cores} cores'
    return heat_unfit(machine, probe) or power_unfit(machine, probe)


def prepare(command, rules):
    """The command with the steps it needs first: each rule {when: regex, run: cmd} whose `when` matches the command (and whose
    `run` is not already in it) puts `run && ` in front. Example: Playwright needs the e2e build or its webServer times out."""
    first = []
    for r in rules or []:
        if re.search(r['when'], command) and r['run'] not in command and r['run'] not in first:
            first.append(r['run'])
    if not first:
        return command
    # grouped: `a; b` or `a || b` as the command must not run when a step failed
    return ' && '.join(f'{{ {c}; }}' for c in [*first, command])


# A targeted run that turns coverage off says nothing about the per-file coverage gate: say so instead of staying silent.
NO_COVERAGE = r'coverage\.enabled[= ]false|--no-cov\b|--coverage=false|COVERAGE=0'


def coverage_note(command, pattern=NO_COVERAGE):
    """A warning line when `command` runs tests with coverage switched off, else ''."""
    if re.search(pattern or NO_COVERAGE, command):
        return 'coverage NOT checked: this run turns coverage off; the per-file coverage gate runs only in the full checks'
    return ''


def refused(machine, probe):
    """A machine never to queue on: paused, off, too hot, or a phone whose battery or charging refuses it."""
    return owner_unfit(machine) or heat_unfit(machine, probe) or power_unfit(machine, probe)


def choose(order, machines, probe_fn):
    """The first fit machine in `order`, probed lazily (a later machine is asked only when the earlier ones are not fit).
    When none is fit the run queues on the first reachable machine whose yield flag is down, else on the first one."""
    if not order:
        raise ValueError('routing.order is empty: name at least one machine')
    seen, why = {}, []
    for name in order:
        seen[name] = probe_fn(name)
        reason = unfit(machines[name], seen[name])
        if not reason:
            note = f'free slots {seen[name].free}' + (f'; {"; ".join(why)}' if why else '')
            return Choice(name, False, note)
        why.append(f'{name}: {reason}')
    # never queue on a machine refused for heat: slot.sh would start the run there as soon as a lock is free
    patient = [n for n in order if seen[n].reachable and not seen[n].yielding and not refused(machines[n], seen[n])]
    if patient:
        return Choice(patient[0], True, '; '.join(why))
    # every machine refused for heat (or unreachable): queue on the coolest reachable one, and say so
    hot = sorted((n for n in order if seen[n].reachable and not seen[n].yielding and not owner_unfit(machines[n])
                  and not power_unfit(machines[n], seen[n])), key=lambda n: seen[n].hottest()[0] or 0)
    if hot:
        return Choice(hot[0], True, '; '.join(why) + f'; every machine is hot: queued on the coolest, {hot[0]}')
    usable = [n for n in order if not owner_unfit(machines[n]) and not power_unfit(machines[n], seen[n])]
    if not usable:
        raise NoMachine(f'no machine may take a run: {"; ".join(why)}; resume one from the dashboard or `tutti resume machine <name>`')
    return Choice(usable[0], True, '; '.join(why))


# ---- which agent account takes the next job, from the latest readings

USE_FIRST = ('late', 'behind')    # quota that will be lost at the reset: spend it first
NORMAL = ('on pace', 'no reading', 'unknown')


def account_unfit(s):
    """Why an account (a pacing.account_state) must not take new work, or ''."""
    if s.get('panic'):
        return f'panic: {s["used"]:g} % used before the target time' if s.get('used') is not None else 'panic'
    if s.get('verdict') == 'spent':
        return 'spent until the reset'
    if s.get('used') is not None and s['used'] >= s.get('panic_at', 95):
        return f'{s["used"]:g} % used: at the panic threshold'
    p = s.get('quota_plan')
    if p and not p.get('limit') and p['left'] <= 0:
        return 'its planned room is used (what is left is kept as its reserve or for its spend outside Tutti)'
    if p and p.get('limit') and p['left'] <= 0:
        if not p['allowance']:
            return 'this window is in the quiet hours (autopilot.quiet_hours)'
        return f'this window used its share of {p["limit"]} ({p["this_window"]:g} of {p["allowance"]:g} points)'
    for lim in s.get('limits') or []:   # a weekly limit spent or in panic closes its 5 h window too
        why = account_unfit(lim)
        if why:
            return f'its limit {lim["name"]}: {why}'
    return ''


def rank(s):
    v = s.get('verdict')
    if v in USE_FIRST:
        return 0
    return 1 if v in NORMAL else 2


def expiry(s):
    """When the account's planned room expires (the planner's window end), or never: soonest first wastes the least."""
    p = s.get('quota_plan')
    return p['expires'] if p and p.get('tasks') else '9999'


def order(s):
    """Accounts ahead of their pace last; among the rest, the capacity that expires soonest first, then the verdict."""
    return (s.get('verdict') == 'ahead', expiry(s), rank(s))


def pick_account(states):
    """(the account state to use, why) from states in preference order: never one in panic or spent; first the ones that
    would lose quota at the reset (late, behind), then on pace or unread, then ahead. (None, why) when none has room."""
    skipped = []
    usable = []
    for s in states:
        reason = account_unfit(s)
        if reason:
            skipped.append(f'{s["name"]}: {reason}')
        else:
            usable.append(s)
    if not usable:
        why = '; '.join(skipped) or 'no account names a worker (accounts.<name>.worker)'
        return None, f'no agent account has room ({why}): use a local model or wait for a reset'
    best = min(usable, key=order)   # min keeps the first of equal rank: the configured order breaks ties
    note = f'{best["name"]} is {best["verdict"]}'
    return best, note + (f'; skipped {"; ".join(skipped)}' if skipped else '')
