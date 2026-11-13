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
