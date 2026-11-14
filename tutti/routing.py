"""Fall-over routing: which machine runs a heavy command. Pure logic; the probes come from the caller."""
from dataclasses import dataclass


@dataclass
class Probe:
    """What one look at a machine found. free: slots nobody holds; yielding: its yield flag is up."""
    reachable: bool = False
    free: int = 0
    load: float = 0.0
    cores: int = 1
    yielding: bool = False


@dataclass
class Choice:
    machine: str
    queued: bool
    reason: str


def parse_probe(text):
    """The probe script prints `<free> <load> <cores> <yield 0|1>`; anything else means the machine did not answer."""
    parts = (text or '').split()
    try:
        return Probe(True, int(parts[0]), float(parts[1]), max(1, int(parts[2])), parts[3] == '1')
    except (IndexError, ValueError):
        return Probe()


def unfit(machine, probe):
    """Why this machine cannot take a run right now, or '' when it can."""
    if not probe.reachable:
        return 'unreachable'
    if probe.yielding:
        return 'yield flag is up'
    if probe.free < 1:
        return 'no free slot'
    ratio = machine.get('max_load_ratio')
    if ratio is not None and probe.load >= ratio * probe.cores:
        return f'load {probe.load:g} is over {ratio:g} x {probe.cores} cores'
    return ''


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
    patient = [n for n in order if seen[n].reachable and not seen[n].yielding]
    return Choice((patient or order)[0], True, '; '.join(why))


# ---- which agent account takes the next job, from the latest readings

USE_FIRST = ('late', 'behind')    # quota that will be lost at the reset: spend it first
NORMAL = ('on pace', 'no reading', 'unknown')


def account_unfit(s):
    """Why an account (a pacing.account_state) must not take new work, or ''."""
    if s.get('panic'):
        return f'panic: {s["used"]:g} % used before the target time'
    if s.get('verdict') == 'spent':
        return 'spent until the reset'
    if s.get('used') is not None and s['used'] >= s.get('panic_at', 95):
        return f'{s["used"]:g} % used: at the panic threshold'
    return ''


def rank(s):
    v = s.get('verdict')
    return 0 if v in USE_FIRST else 1 if v in NORMAL else 2


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
    best = min(usable, key=rank)   # min keeps the first of equal rank: the configured order breaks ties
    note = f'{best["name"]} is {best["verdict"]}'
    return best, note + (f'; skipped {"; ".join(skipped)}' if skipped else '')
