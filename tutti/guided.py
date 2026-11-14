"""The rule every guided setup in Tutti follows: `tutti setup`, `household join`, `connect`, `vpn setup`, `move`
(docs/INSTALL.md, *The rule for every setup*). The owner's words: minimum input, and none at all is best.

1. Detect everything that can be detected.
2. For the rest, pick the recommended default and record it as a Choice: the dashboard's Set up card shows it with a
   Change link (its config key), so nothing is asked while setup runs.
3. Only what no tool can do for the person is left: a sign-in in a tool's own window, a one-line sudo command, a tap on a
   phone. These are Steps, collected while setup runs and shown once, as one numbered list, at the end.

A feature's setup returns (choices, steps); `tutti setup` merges them into its report (<state>/setup.json)."""

KINDS = ('sign-in', 'sudo', 'phone', 'other')


def choice(key, value, why):
    """A default picked for the person: `key` is the config key that changes it."""
    return {'key': key, 'value': value, 'why': why}


def step(kind, title, command, why):
    """What only the person can do. kind: sign-in (the tool's own window opens), sudo (one line to paste), phone (a tap)."""
    if kind not in KINDS:
        raise ValueError(f'step kind {kind!r}: one of {", ".join(KINDS)}')
    return {'kind': kind, 'title': title, 'command': command, 'why': why}


def render(steps):
    """The one list at the end: numbered, sign-ins first, each with its exact command."""
    if not steps:
        return 'Nothing left for you: everything is set up.'
    order = sorted(steps, key=lambda s: KINDS.index(s['kind']))
    lines = [f'Left for you ({len(order)}): only you can do these.']
    for i, s in enumerate(order, 1):
        lines.append(f'  {i}. {s["title"]}: {s["why"]}' + (f'\n     {s["command"]}' if s['command'] else ''))
    return '\n'.join(lines)


def choice_rows(choices):
    """The Set up card's rows for the defaults picked: state 'default' and the key its Change link opens."""
    return [{'id': f'default-{c["key"]}', 'title': c['key'], 'state': 'default', 'detail': f'{c["value"]}: {c["why"]}',
             'command': None, 'fix': None, 'change': c['key']} for c in choices]
