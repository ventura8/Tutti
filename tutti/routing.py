"""Fall-over routing: which machine runs a heavy command. Pure logic; the probes come from the caller."""
import math
import re
from dataclasses import dataclass, field

from . import controls, devices, hygiene


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
    capped_cpus: float = 0.0       # CPUs of the machine's governor-capped containers (`capped`): load that gives way, not load
    zone: str = ''                 # the machine's own IANA zone, read over its ssh (tutti/tz.py): its quiet hours are its local time
    mem_gb: float | None = None    # free memory (MemAvailable) in GB, None when unreadable
    idle_s: float | None = None    # seconds since a person last used it (graphical session, Windows console); None: nobody
    disk_gb: float | None = None   # the least free space on its work disks (home, a WSL machine's C:), None when unreadable

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


def _field(word):
    try:
        float(word)
    except ValueError:
        return word == '-'
    return True


def without_gpu_text(parts):
    """The probe line's fields with a GPU tool's error text (nvidia-smi without a driver prints it on stdout) taken out of the
    GPU field and replaced by `-`: the fields after it (free memory, idle time) keep their places."""
    if len(parts) <= 5 or _field(parts[5]):
        return parts
    end = 5
    while end < len(parts) and not _field(parts[end]):
        end += 1
    return parts[:5] + ['-'] + parts[end:]


def parse_probe(text):
    """The probe script prints `<free> <load> <cores> <yield 0|1> [<millidegrees> [<GPU degrees or ->]]`; anything else means
    the machine did not answer. The temperatures are optional: an older probe (or a machine without sensors) leaves them out."""
    parts = without_gpu_text((text or '').split())
    try:
        temp = parse_temp(parts[4] if len(parts) > 4 else None)
        gpu = parse_gpu_temp(parts[5] if len(parts) > 5 else None)
        mem = float(parts[6]) if len(parts) > 6 and parts[6] != '-' else None
        idle = float(parts[7]) if len(parts) > 7 and parts[7] != '-' else None
        disk = float(parts[8]) if len(parts) > 8 and parts[8] != '-' else None
        return Probe(True, int(parts[0]), float(parts[1]), max(1, int(parts[2])), parts[3] == '1', temp, gpu_temp=gpu, mem_gb=mem,
                     idle_s=idle, disk_gb=disk)
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


def limit_unfit(machine, now, heavy=False):
    """Why the machine's limited-hours window refuses a run, or ''. Heavy work (`tutti test`, routed checks, e2e) never
    starts inside the window (docs/FLEET.md): a Plex/media box keeps its evening for people. Light work still uses the
    reduced share. `now` is a UTC datetime; None skips the check (unit tests that only care about heat/slots)."""
    if now is None or not heavy:
        return ''
    from . import share
    cap = share.limited(machine, now)
    if cap is None:
        return ''
    window = (machine.get('limit') or {}).get('window') or '?'
    return f'limited hours {window} at {cap} %: heavy work goes elsewhere (media/Plex window)'


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


def unfit(machine, probe, now=None, heavy=False):
    """Why this machine cannot take a run right now, or '' when it can."""
    owner = owner_unfit(machine)
    if owner:
        return owner
    if not probe.reachable:
        return 'unreachable'
    if probe.yielding:
        return 'yield flag is up'
    limited = limit_unfit(machine, now, heavy)
    if limited:
        return limited
    if probe.free < 1:
        return 'no free slot'
    if probe.disk_gb is not None and probe.disk_gb < hygiene.STOP_GB:
        return (f'disk nearly full: {probe.disk_gb:g} GB free, nothing new under {hygiene.STOP_GB} GB (its clean-up runs by itself; '
                'the biggest items are on its machine card)')
    ratio = machine.get('max_load_ratio')
    load = max(0.0, probe.load - (getattr(probe, 'capped_cpus', 0.0) or 0.0))
    if ratio is not None and load >= ratio * probe.cores:
        return f'load {load:g} is over {ratio:g} x {probe.cores} cores' + (f' ({probe.capped_cpus:g} capped not counted)'
                                                                           if probe.capped_cpus else '')
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


def refused(machine, probe, now=None, heavy=False):
    """A machine never to queue on: paused, off, too hot, limited for heavy work, or a phone whose power policy refuses it."""
    return (owner_unfit(machine) or heat_unfit(machine, probe) or power_unfit(machine, probe)
            or limit_unfit(machine, now, heavy))


def choose(order, machines, probe_fn, now=None, heavy=False):
    """The first fit machine in `order`, probed lazily (a later machine is asked only when the earlier ones are not fit).
    When none is fit the run queues on the first reachable machine whose yield flag is down, else on the first one.
    `heavy`: a Tutti test / routed check / e2e — never starts on a machine inside its limited-hours window (t444)."""
    if not order:
        raise ValueError('routing.order is empty: name at least one machine')
    seen, why = {}, []
    for name in order:
        seen[name] = probe_fn(name)
        reason = unfit(machines[name], seen[name], now=now, heavy=heavy)
        if not reason:
            note = f'free slots {seen[name].free}' + (f'; {"; ".join(why)}' if why else '')
            return Choice(name, False, note)
        why.append(f'{name}: {reason}')
    # never queue on a machine refused for heat or limited heavy: slot.sh would start as soon as a lock is free
    patient = [n for n in order if seen[n].reachable and not seen[n].yielding
               and not refused(machines[n], seen[n], now=now, heavy=heavy)]
    if patient:
        return Choice(patient[0], True, '; '.join(why))
    # every machine refused for heat (or unreachable): queue on the coolest reachable one, and say so
    hot = sorted((n for n in order if seen[n].reachable and not seen[n].yielding and not owner_unfit(machines[n])
                  and not power_unfit(machines[n], seen[n]) and not limit_unfit(machines[n], now, heavy)),
                 key=lambda n: seen[n].hottest()[0] or 0)
    if hot:
        return Choice(hot[0], True, '; '.join(why) + f'; every machine is hot: queued on the coolest, {hot[0]}')
    usable = [n for n in order if not owner_unfit(machines[n]) and not power_unfit(machines[n], seen[n])
              and not limit_unfit(machines[n], now, heavy)]
    if not usable:
        raise NoMachine(f'no machine may take a run: {"; ".join(why)}; resume one from the dashboard or `tutti resume machine <name>`')
    return Choice(usable[0], True, '; '.join(why))

# ---- which agent account takes the next job, from the latest readings

USE_FIRST = ('late', 'behind')    # quota that will be lost at the reset: spend it first
NORMAL = ('on pace', 'no reading', 'unknown')


def missing_limits(cfg, name, state):
    """Configured linked quota windows absent from this account's pacing state, including paused accounts."""
    limits = cfg.get('_account_limits') or {n: (a or {}).get('limits') or [] for n, a in (cfg.get('accounts') or {}).items()}
    expected, pending = set(), list(limits.get(name) or [])
    while pending:
        n = pending.pop()
        if n not in expected:
            expected.add(n)
            pending.extend(limits.get(n) or [])
    found, pending_states = set(), list(state.get('limits') or [])
    while pending_states:
        item = pending_states.pop()
        if isinstance(item, dict):
            found.add(item.get('name'))
            pending_states.extend(item.get('limits') or [])
    return sorted(expected - found)


def invalid_usage(s):
    used = s.get('used')
    return used is not None and (isinstance(used, bool) or not isinstance(used, (int, float)) or
                                 not math.isfinite(used) or not 0 <= used <= 100)


def quota_unfit(s):
    """Why the account's own observed quota cannot be used, or ''."""
    if s.get('_missing_limits'):
        return 'missing linked quota state: ' + ', '.join(s['_missing_limits'])
    if invalid_usage(s):
        return 'invalid quota reading: refresh this account before using it'
    if s.get('panic'):
        for lim in s.get('limits') or []:
            if lim.get('name') == s.get('bound_by'):
                why = quota_unfit(lim)
                if why:
                    return f'its limit {lim["name"]}: {why}'
        return f'panic: {s["used"]:g} % used before the target time' if s.get('used') is not None else 'panic'
    if s.get('verdict') == 'spent':
        return 'spent until the reset'
    if s.get('used') is not None and s['used'] >= s.get('panic_at', 95):
        return f'{s["used"]:g} % used: at the panic threshold'
    return ''


def account_unfit(s):
    """Why an account (a pacing.account_state) must not take new work, or ''."""
    reason = quota_unfit(s)
    if reason:
        return reason
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


def quota_closed(s):
    """An account or any linked window is in panic or spent, independent of pacing reserves."""
    used = s.get('used')
    return bool(s.get('_missing_limits') or invalid_usage(s) or s.get('panic') or s.get('verdict') == 'spent' or
                (used is not None and used >= s.get('panic_at', 95)) or any(quota_closed(x) for x in s.get('limits') or []))


def unknown_quota(s):
    """A takeover must not assume an unmeasured account or linked quota has room."""
    return s.get('used') is None or invalid_usage(s) or any(unknown_quota(x) for x in s.get('limits') or [])


def pick_account(states, takeover=False):
    """(the account state to use, why) from states in preference order: never one in panic or spent; first the ones that
    would lose quota at the reset (late, behind), then on pace or unread, then ahead. (None, why) when none has room."""
    skipped = []
    usable = []
    for s in states:
        reason = account_unfit(s)
        if takeover and unknown_quota(s) and not quota_closed(s):
            reason = 'quota is unknown: refresh the account and all linked limits before taking over'
        elif takeover and reason and not quota_closed(s):
            reason = ''   # the main agent panics: only an account's own panic or a spent quota stops it (autopilot.takes_over)
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
