"""Each account's target: the % of its quota it should have used by its spend-by time (reset minus `lead_hours`):
`accounts.<name>.target_pct` (default 100), `main.target_pct` for the main agent's accounts (the owner's reserve still keeps
the owner's working hours; the target only says where the spend ends). The account card's line says where the account is
heading: "Target 100 % · on track to finish at 97 % by 23:00", and when it falls short, why (no work queued, the machines or
devices busy, the account blocked) and what Tutti already does about it (fill_room, more parallel runs, a stronger model
while behind) before it warns. Pure."""
from . import tz
from .pacing import parse_time

SHORT_MARGIN = 5   # points under the target that count as falling short


def projected(s, now):
    """The % the account is heading for at its spend-by time: its last day's rate carried on, else where it is now."""
    from . import owner
    if s.get('used') is None or not s.get('target'):
        return None
    end = owner.projection(s.get('readings') or [], s['used'], now, parse_time(s['target']))
    return round(min(100.0, s['used'] if end is None else end), 1)


def blockers(s, facts):
    """Why the account would fall short, in plain words: the first that applies."""
    if s.get('panic') or s.get('bound_by'):
        return f'the account is blocked ({s.get("bound_by") or "panic"})'
    if not facts.get('queued'):
        return 'no work queued for it'
    if facts.get('busy'):
        return f'the machines or devices are busy ({facts["busy"]})'
    return 'its pace is too slow for the work queued'


def acting(facts):
    out = []
    if facts.get('fill_room'):
        out.append('fill_room adds tasks')
    if facts.get('parallel'):
        out.append(f'up to {facts["parallel"]} runs at once')
    out.append('a stronger model while behind')
    return ', '.join(out)


def outlook(s, now, facts):
    """{target, projected, by, short, text} for one account state `s`; `facts`: {queued, busy, fill_room, parallel}."""
    if s.get('state') != 'ok' or s.get('used') is None:
        return None
    goal = float(s.get('target_pct') or 100)
    end = projected(s, now)
    out = (s.get('quota_plan') or {}).get('run_out')
    if out:   # hard safety outranks the target: the owner's main agent must never run out because of the workers
        at = parse_time(out)
        return {'target': goal, 'projected': 100.0, 'by': s['target'], 'short': 'run out',
                'text': f'At today\'s pace you run out on {tz.show(at)}, before the reset, even with no worker on it. '
                        'Done: no worker tasks on this account; their work goes to the other accounts and local models. '
                        'Slow down, or set a lower main.target_pct.'}
    by = parse_time(s['target'])
    when = tz.show(by, now=now, date=False)
    end = None if end is None else round(end, 1)   # a sum of shares leaves float noise (32.122999999999976)
    head = f'Target {goal:g} % · on track to finish at {end:g} % by {when}'
    if end is None or end >= goal - SHORT_MARGIN or s.get('scout'):
        return {'target': goal, 'projected': end, 'by': s['target'], 'short': '', 'text': head}
    why = blockers(s, facts)
    return {'target': goal, 'projected': end, 'by': s['target'], 'short': why,
            'text': f'Target {goal:g} % · heading for {end:g} % by {when}: short because {why}; acting: {acting(facts)}'}
