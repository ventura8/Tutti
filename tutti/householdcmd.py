"""`tutti user` and `tutti household`: who this Tutti works for, and the household it shares machines and projects with
(docs/HOUSEHOLD.md)."""
import sys

from . import telemetry, users


def say(msg):
    sys.stderr.write(f'tutti: {msg}\n')


def print_user(v, out=sys.stdout):
    role = {'alone': 'a household of one', 'member': f'member of {v["household"]}',
            'coordinator': f'coordinator of {v["household"]}'}[v['role']]
    print(f'user: {v["name"]} (id {v["hash"]}); main agent {v["main"] or "(none)"}; {role}', file=out)
    print(f'shares spare room of: {", ".join(v["shares"]) or "no account (accounts.<n>.share_spare)"}', file=out)
    print(f'config: {v["config"]}', file=out)
    print(f'state: {v["state"]}', file=out)


def cmd_user(cfg, a, out=sys.stdout):
    """`tutti user [show|name <name>|share <account> on|off]`."""
    try:
        if a.action == 'name':
            users.set_name(cfg, a.arg)
        elif a.action == 'share':
            if a.value not in ('on', 'off'):
                say('user share: say on or off, e.g. `tutti user share agy-claude on` (room it would lose at its reset '
                    'may do the household\'s shared tasks)')
                return 2
            users.set_share(cfg, a.arg or '', a.value == 'on')
    except users.UserError as e:
        say(str(e))
        return 2
    v = users.view(cfg)
    telemetry.record('feature', f'user.{a.action}', user=v['hash'], role=v['role'])
    print_user(v, out)
    return 0


def cmd_household(cfg, a, out=sys.stdout):
    """`tutti household status` (the coordinator and the other actions come with it: docs/HOUSEHOLD.md)."""
    v = users.view(cfg)
    if v['role'] == 'alone':
        print(f'household of one: {v["name"]}. Nothing is shared: to share machines and projects with others in your home, '
              'run `tutti household init` on an always-on computer (docs/HOUSEHOLD.md)', file=out)
        return 0
    print(f'household {v["household"]}: {v["name"]} is a {v["role"]}', file=out)
    return 0
