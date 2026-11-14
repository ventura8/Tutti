"""`tutti user` and `tutti household`: who this Tutti works for, and the household it shares machines and projects with
(docs/HOUSEHOLD.md). Guided, minimum input: init asks only the household's name, join only the code (the name and main
agent default to `tutti user`'s and are asked for on a terminal)."""
import json
import sys
import time

from . import config, coordinator, household, sharing, telemetry, users

ACTIONS = ('status', 'init', 'join', 'leave', 'code', 'serve', 'project')


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


def ask(prompt, default, io):
    """A terminal question with a default (Enter keeps it); without a terminal the default, asked nothing."""
    if not io.get('isatty', lambda: sys.stdin.isatty())():
        return default
    answer = io.get('input', input)(f'{prompt} [{default}]: ').strip()
    return answer or default


def ago(seconds):
    if seconds < 90:
        return 'now'
    return f'{seconds / 60:.0f} min ago' if seconds < 5400 else f'{seconds / 3600:.0f} h ago'


def presence(m, now):
    if m['active']:
        return 'prompting'
    return 'online' if m['online'] else f'last seen {ago(now - (m["last_seen"] or 0))}'


def print_status(v, me, out, now):
    print(f'household {v["household"]} (admin {v["admin"] or "?"}); you are {me["name"]}', file=out)
    for m in v['members']:
        print(f'  {m["name"]:16} {m["agent"] or "?":10} {presence(m, now):22} {m["runs_today"]} run(s) today', file=out)
    for pkey, counts in sorted(v['projects'].items()):
        print(f'  shared project {pkey}: ' + ', '.join(f'{n} {s}' for s, n in sorted(counts.items())), file=out)
    print(f'  leases: {len(v["my_leases"])} yours, {v["other_leases"]} held by others', file=out)


def status(cfg, a, out, client_fn):
    me = users.view(cfg)
    if me['role'] == 'alone':
        print(f'household of one: {me["name"]}. Nothing is shared: to share machines and projects with others in your home, '
              'run `tutti household init` on an always-on computer (docs/HOUSEHOLD.md)', file=out)
        return 0
    try:
        v = client_fn(cfg).call('/hh/status')
    except coordinator.Unreachable as e:
        print(f'household {me["household"]}: {e}. Working alone: your own repos and machines as usual, shared projects wait. '
              'Check that the coordinator\'s computer is on and its dashboard (or `tutti household serve`) runs.', file=out)
        return 1
    if a.json:
        print(json.dumps(v, indent=1), file=out)
    else:
        print_status(v, me, out, time.time())
    return 0


def init(cfg, a, out, io):
    name = a.name or ask('The household\'s name', 'home', io)
    why = users.name_error(name)
    if why:
        raise household.HouseholdError(why.replace('user name', 'household name'))
    code = coordinator.init(cfg, name, a.host, a.port)
    telemetry.record('feature', 'household.init', user=users.identity(cfg)['hash'], outcome='ok')
    print(f'This computer coordinates the household "{name}". Its dashboard serves it (keep `tutti dashboard` running, or run '
          '`tutti household serve`).', file=out)
    print(f'\nEach other member runs, in their own login (valid {household.CODE_DAYS} days):\n\n'
          f'    tutti household join {code}\n', file=out)
    return 0


def join(cfg, a, out, io, join_fn):
    if not a.arg:
        say('household join: give the code from the coordinator, e.g. `tutti household join tutti-hh:mini.lan:4778:XXXX-...`')
        return 2
    me = users.identity(cfg)
    name = a.name or ask('Your name in the household', me['name'], io)
    agent = telemetry.tool_of(me['main'] or '')
    try:
        answer = join_fn(cfg, a.arg[0], name, agent)
    except coordinator.Unreachable as e:
        telemetry.record('feature', 'household.join', user=me['hash'], outcome='offline')
        say(f'{e}: check that the coordinator\'s computer is on, on this network, and its dashboard (or `tutti household serve`) '
            'runs; then try again')
        return 1
    telemetry.record('feature', 'household.join', user=me['hash'], outcome='ok')
    print(f'Joined the household "{answer["household"]}" as {answer["name"]}. Nothing is shared yet: `tutti household project '
          'share <repo>` shares a project\'s backlog, and the shared machines come with the next release.', file=out)
    return 0


def project(cfg, a, out):
    if len(a.arg) != 2 or a.arg[0] not in ('share', 'unshare'):
        say('household project: say share or unshare and the repo, e.g. `tutti household project share app`')
        return 2
    if a.arg[0] == 'share' and a.arg[1] in (cfg['autopilot'].get('repos') or {}) and not sharing.key_of_repo(cfg, a.arg[1]):
        say(f'household project: {a.arg[1]} has no origin remote, so other members\' copies cannot be matched to it: give it '
            'one (git remote add origin <url>), then share it')
        return 2
    sharing.set_shared(cfg, a.arg[1], a.arg[0] == 'share')
    print(f'{a.arg[1]}: {"shared with the household" if a.arg[0] == "share" else "private again"}; shared repos: '
          f'{", ".join(sharing.shared_repos(cfg)) or "(none)"}', file=out)
    return 0


def serve(cfg, a, out):
    if users.read_household().get('role') != 'coordinator':
        say('household serve: this login is not a household coordinator: `tutti household init` first')
        return 2
    m = users.read_household()
    port = a.port or int(m['coordinator'].rsplit(':', 1)[1])
    httpd = coordinator.server(cfg, port)
    if a.port:   # members and this login reach it at the new port from now on; new join codes name it
        users.write_household({**m, 'coordinator': f'127.0.0.1:{port}'})
        coordinator.set_port(cfg, port)
    print(f'household coordinator: on port {httpd.server_address[1]} (Ctrl+C stops it)', file=out)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return 0


def leave(cfg, out, client_fn):
    """Leaves the household; 1 when the coordinator could not be told (the membership ends here anyway)."""
    m = users.read_household()
    if not m:
        print('household of one already: nothing to leave', file=out)
        return 0
    rc = 0
    try:
        client_fn(cfg).call('/hh/leave')
        told = 'the coordinator knows'
    except (coordinator.Unreachable, household.HouseholdError) as e:
        told, rc = f'the coordinator was not told ({e}): its admin can remove you', 1
    users.leave_household()
    users.write(config.state_dir(cfg), shared_repos=None)   # a later join starts with nothing shared
    print(f'Left the household "{m.get("household")}" ({told}). Your own setup is as before; shared repos are private again.',
          file=out)
    return rc


def cmd_household(cfg, a, out=sys.stdout, io=None, client_fn=coordinator.client_of, join_fn=coordinator.join):
    """`tutti household status|init|join|leave|code|serve|project` (docs/HOUSEHOLD.md)."""
    io = io or {}
    try:
        if a.action == 'status':
            return status(cfg, a, out, client_fn)
        if a.action == 'init':
            return init(cfg, a, out, io)
        if a.action == 'join':
            return join(cfg, a, out, io, join_fn)
        if a.action == 'leave':
            return leave(cfg, out, client_fn)
        if a.action == 'project':
            return project(cfg, a, out)
        if a.action == 'serve':
            return serve(cfg, a, out)
        if users.read_household().get('role') != 'coordinator':
            say('household code: only the coordinator\'s computer makes join codes: run it there')
            return 2
        print(f'New join code (the old one stops working):\n\n    tutti household join {client_fn(cfg).call("/hh/code")["code"]}\n',
              file=out)
        return 0
    except (household.HouseholdError, users.UserError) as e:
        say(str(e))
        return 2
    except coordinator.Unreachable as e:
        say(f'{e}: start the dashboard (or `tutti household serve`) on this computer, then try again')
        return 1
