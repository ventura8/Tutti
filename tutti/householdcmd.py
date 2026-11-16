"""`tutti user` and `tutti household`: who this Tutti works for, and the household it shares machines and projects with
(docs/HOUSEHOLD.md). Guided, minimum input: init asks only the household's name, join only the code (the name and main
agent default to `tutti user`'s and are asked for on a terminal)."""
import json
import sys
import time

from . import config, coordinator, guided, hhjobs, household, netpolicy, sharing, telemetry, users

ACTIONS = ('status', 'init', 'join', 'leave', 'code', 'serve', 'project', 'machine', 'job')
MACHINE_ACTIONS = ('share', 'unshare', 'use', 'pause', 'resume')


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
        if a.action == 'notify':
            return notify(cfg, a, out)
        if a.action == 'main-machine':
            from . import mainmachine
            return mainmachine.change(cfg, a.arg or '', dry_run=bool(getattr(a, 'dry_run', False)), out=out)
        if a.action == 'main-agent':
            worker, note = users.set_main_agent(cfg, a.arg or '')
            print(f'main agent: {worker}' + (f' ({note})' if note else ''), file=out)
            cfg['main']['agent'] = worker
        elif a.action == 'name':
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
    except RuntimeError as e:   # mainmachine.MainMachineError
        say(str(e))
        return 2
    v = users.view(cfg)
    telemetry.record('feature', f'user.{a.action}', user=v['hash'], role=v['role'])
    print_user(v, out)
    return 0


def notify(cfg, a, out):
    """`tutti user notify always|work-hours [--batch N] [--quiet HH:MM-HH:MM] [--for <email>]`."""
    from . import auth, needs
    changes = {k: v for k, v in (('notify', a.arg), ('batch', a.batch), ('quiet', a.quiet)) if v is not None}
    key = needs.OWNER
    if a.for_user:
        found = [s for s, u in auth.Users(config.state_dir(cfg)).all().items() if (u.get('email') or '').lower() == a.for_user.lower()]
        if not found:
            say(f'user notify: no signed-in user {a.for_user}: `tutti auth users` lists them')
            return 2
        key = found[0]
    ok, res = needs.set_user(cfg, key, changes)
    if not ok:
        say(f'user notify: {res}')
        return 2
    if a.for_user:   # audited once the change is made (the owner at this computer's terminal)
        auth.audit(config.state_dir(cfg), {'sub': needs.OWNER, 'email': 'tutti user notify'}, 'notify',
                   {'sub': key, 'email': a.for_user}, detail=', '.join(f'{k}={v}' for k, v in changes.items()))
    print(f'questions: {res["notify"]} (outside work hours once {res["batch"]} wait, urgent at once; quiet {res["quiet"] or "none"})'
          if res['notify'] == 'always' else 'questions: in work hours; the rest wait for your welcome-back summary', file=out)
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
    for name, m in sorted((v.get('machines') or {}).items()):
        use = ', '.join(f'{n} {k}' for n, k in sorted(m['use'].items())) or 'idle'
        paused = f'; paused by {m["paused_by"]}' if m['paused'] else ''
        print(f'  machine {name} ({m["owner"]}\'s, {m["slots"]} slot(s)): {use}; your share now {m["grant"]}{paused}', file=out)


def status(cfg, a, out, client_fn):
    me = users.view(cfg)
    if me['role'] == 'alone':
        print(f'household of one: {me["name"]}. Nothing is shared: to share machines and projects with others in your home, '
              'run `tutti household init` on an always-on computer (docs/HOUSEHOLD.md)', file=out)
        return 0
    try:
        c = client_fn(cfg)
        v = {**c.call('/hh/status'), 'machines': c.call('/hh/sync', {'beat': {}}).get('machines')}
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


MAIN_KEY = 'main.agent'
JOIN_EVENT = 'household.join'
PREFERRED = ('claude', 'codex', 'cursor', 'agy', 'copilot', 'gemini', 'local')   # the default when several are installed


def detect_main(cfg, which=None):
    """(kind, guided choice): the main agent, never asked (docs/INSTALL.md, *The rule*): the config's when its program is
    installed, else the installed one (the first by PREFERRED when several), else claude. The choice says why, and
    `tutti user main-agent <kind>` changes it."""
    import shutil
    which = which or shutil.which
    installed = [k for k in PREFERRED if any(which(n) for n in users.MAIN_KINDS[k][0])]
    current = telemetry.tool_of((cfg.get('main') or {}).get('agent') or '')
    current = {'antigravity': 'agy'}.get(current, current)
    if current in installed:
        return current, guided.choice(MAIN_KEY, current, 'the main agent in your config, installed here')
    if installed:
        why = 'the only agent installed here' if len(installed) == 1 else f'installed here (also: {", ".join(installed[1:])})'
        return installed[0], guided.choice(MAIN_KEY, installed[0], why + '; `tutti user main-agent <kind>` changes it')
    return 'claude', guided.choice(MAIN_KEY, 'claude', 'no agent is installed yet; `tutti setup` finds the one you install')


def subscriptions(cfg):
    """The kinds of the accounts in this config (the readings and names stay here): what the household view shows."""
    return sorted({telemetry.tool_of(n) for n in cfg['accounts']} - {'other', 'none'})


def join_invite(cfg, a, out, invite_fn):
    """`tutti household join <invite-url>`: one command, under a minute, nothing asked (docs/INSTALL.md, *The rule*):
    detect the main agent and subscriptions, default the rest, pair with the Google identity of the invite's sign-in, and
    list once at the end what only the person can do."""
    started = time.time()
    kind, picked = detect_main(cfg)
    steps = []
    try:
        answer = invite_fn(cfg, a.arg[0], kind, subscriptions(cfg), override=a.host, say=say)
    except coordinator.Unreachable as e:
        say(f'{e}: the household is served by the dashboard that made the invite; check it runs and this computer is on '
            'the same network (or the household VPN), then run the same command again')
        return 1
    except household.HouseholdError as e:
        if 'sign in with Google' not in str(e):
            raise
        steps.append(guided.step('sign-in', 'Sign in with Google on the invite page', a.arg[0],
                                 'your household identity is your Google account; then run the same command again'))
        print(guided.render(steps), file=out)
        return 1
    if not (cfg.get('user') or {}).get('name') and not users.name_error(answer['name']):
        users.write(config.state_dir(cfg), name=answer['name'])
    note = ''
    if users.worker_for(cfg, kind) and telemetry.tool_of(cfg['main'].get('agent') or '') != kind:
        note = users.set_main_agent(cfg, kind)[1]
    elif not users.worker_for(cfg, kind):
        steps.append(guided.step('other', f'Sign in to {kind} once', 'tutti setup',
                                 f'no account of {kind} is in your config yet: `tutti setup` adds it and opens its sign-in'))
    telemetry.record('feature', JOIN_EVENT, user=users.id_hash(answer['sub']), outcome='ok', how='invite')
    print(f'Joined "{answer["household"]}" as {answer["name"]} ({answer.get("email") or "your Google account"}) in '
          f'{time.time() - started:.0f} s. Main agent: {kind} ({picked["why"]})' + (f'; {note}' if note else '') +
          f'; subscriptions: {", ".join(subscriptions(cfg)) or "none yet"}.', file=out)
    print(guided.render(steps), file=out)
    return 0


def invite_lines(invite):
    """The one install line per platform a household invite page shows a new member (docs/INSTALL.md): install, set up
    with no questions, and join, in one paste."""
    from . import install
    return {p: install.install_line(p, join=invite) for p in ('linux', 'darwin', 'win32')}


def join(cfg, a, out, io, join_fn, invite_fn=None):
    if a.arg and '/invite#' in a.arg[0]:
        return join_invite(cfg, a, out, invite_fn or coordinator.join_invite)
    if not a.arg:
        say('household join: give the invite link from the dashboard\'s Users card (Invite someone), e.g. '
            '`tutti household join "https://<dashboard>/invite#..."`, quoted: a shell drops # and what follows (an older '
            'tutti-hh:... code works too)')
        return 2
    me = users.identity(cfg)
    name = a.name or ask('Your name in the household', me['name'], io)
    agent = telemetry.tool_of(me['main'] or '')
    try:
        answer = join_fn(cfg, a.arg[0], name, agent)
    except coordinator.Unreachable as e:
        telemetry.record('feature', JOIN_EVENT, user=me['hash'], outcome='offline')
        say(f'{e}: check that the coordinator\'s computer is on, on this network, and its dashboard (or `tutti household serve`) '
            'runs; then try again')
        return 1
    telemetry.record('feature', JOIN_EVENT, user=me['hash'], outcome='ok')
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


def share_info(cfg, name, host, probe_fn, sh_fn):
    """What the household learns of a machine this member shares; the shared lock and yield folder made on it."""
    m = cfg['machines'][name]
    d = household.SHARED_DIR
    # sticky (1777, like /tmp) and a real folder: in a folder another login made without the sticky bit, a planted link
    # could make every member's lock open a file of theirs
    rc = sh_fn(m, f'mkdir -p {d} && chmod 1777 {d} 2>/dev/null; test -w {d} && test -k {d} && test ! -L {d}')
    if rc.returncode != 0:
        raise household.HouseholdError(f'could not make {d} on {name} (the folder every member\'s login shares locks in, '
                                       'mode 1777): check that it answers over ssh (`tutti machines`); if the folder exists '
                                       f'without the sticky bit or is a link, someone else made it: remove it there, then try '
                                       'again')
    p = probe_fn(m)
    if not host:
        host = coordinator.lan_address() if m['transport'] == 'local' else m.get('host')
    return {'host': host, 'slots': m['slots'], 'kind': m['transport'], 'gpu': p.gpu_temp is not None,
            'lock_prefix': f'{d}/slot-'}


def machine(cfg, a, out, client_fn, probe_fn=None, sh_fn=None):
    """`tutti household machine share|unshare|use|pause|resume <m>`."""
    from . import remote
    probe_fn, sh_fn = probe_fn or remote.probe, sh_fn or (lambda m, script: remote.sh(m, script, capture=True, timeout=30))
    if len(a.arg) != 2 or a.arg[0] not in MACHINE_ACTIONS:
        say('household machine: say share, unshare, use, pause or resume and the machine, e.g. `tutti household machine share pc`')
        return 2
    action, name = a.arg
    folder = config.state_dir(cfg)
    c = client_fn(cfg)
    if action == 'use':
        return use_machine(cfg, name, a, out, c, probe_fn)
    if action == 'share':
        if name not in cfg['machines']:
            names = ', '.join(cfg['machines']) or '(none)'
            raise household.HouseholdError(f'no machine "{name}" in {cfg["_path"]}: the machines are {names}')
        info = share_info(cfg, name, a.host, probe_fn, sh_fn)
        c.call('/hh/machine', {'action': 'share', 'name': name, 'info': info})
        over = dict(users.read(folder).get('machine_overrides') or {})
        over[name] = {'lock_prefix': info['lock_prefix'], 'household_shared': True}
        users.write(folder, machine_overrides=over)
        print(f'{name} is shared with the household at {info["host"]} ({info["slots"]} slot(s)). Others add it with '
              f'`tutti household machine use {name}`; you keep priority only as everyone does: by fair share.', file=out)
        return 0
    c.call('/hh/machine', {'action': action, 'name': name})
    if action == 'unshare':
        over = dict(users.read(folder).get('machine_overrides') or {})
        over.pop(name, None)
        users.write(folder, machine_overrides=over)
    print(f'{name}: {action}d for the household', file=out)
    return 0


def use_machine(cfg, name, a, out, c, probe_fn):
    """Adds another member's shared machine to this member's machines (user.json, never the config file). No login there:
    its owner's Tutti runs this member's tests on it (tutti/hhjobs.py); nothing to set up on the machine."""
    view = (c.call('/hh/sync', {'beat': {}}).get('machines') or {}).get(name)
    if not view:
        raise household.HouseholdError(f'the household has no machine "{name}": `tutti household status` lists them')
    if view['mine']:
        print(f'{name} is your own machine: it is already in your config.', file=out)
        return 0
    if name in cfg['machines'] and cfg['machines'][name]['transport'] != hhjobs.TRANSPORT:
        raise household.HouseholdError(f'you already have a machine "{name}" in {cfg["_path"]}: rename yours to use the '
                                       'household\'s')
    folder = config.state_dir(cfg)
    used = dict(users.read(folder).get('household_machines') or {})
    used[name] = {'transport': hhjobs.TRANSPORT, 'slots': view['slots'], 'household_owner': view['owner'],
                  'household_state': folder,
                  # its model server, when the owner shares one: Tutti's own AI may use the best free model there (tutti/brain.py)
                  **{k: view[k] for k in ('host', 'ollama_port', 'gpu') if view.get(k)}}
    users.write(folder, household_machines=used)
    print(f'{name} ({view["owner"]}\'s) is now one of your machines for tests, gates and builds: {view["owner"]}\'s Tutti '
          'runs them there (your code goes as a WIP ref of the repository; no login or credential of yours goes to the '
          'machine). Routing gives you your fair share of it.', file=out)
    return 0


def serve(cfg, a, out):
    if users.read_household().get('role') != 'coordinator':
        say('household serve: this login is not a household coordinator: `tutti household init` first')
        return 2
    m = users.read_household()
    port = a.port or int(m['coordinator'].rsplit(':', 1)[1])
    all_ = coordinator.servers(cfg, port)
    httpd = all_[0]
    import threading
    for extra in all_[1:]:
        threading.Thread(target=extra.serve_forever, daemon=True).start()
    if a.port:   # members and this login reach it at the new port from now on; new join codes name it
        users.write_household({**m, 'coordinator': f'127.0.0.1:{port}'})
        coordinator.set_port(cfg, port)
    print(f'household coordinator: on {", ".join(h.server_address[0] for h in all_)}, port {httpd.server_address[1]} (LAN and '
          'VPN only; Ctrl+C stops it)', file=out)
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


def code(cfg, out, client_fn):
    if users.read_household().get('role') != 'coordinator':
        say('household code: only the coordinator\'s computer makes join codes: run it there')
        return 2
    print(f'New join code (the old one stops working):\n\n    tutti household join {client_fn(cfg).call("/hh/code")["code"]}\n',
          file=out)
    return 0


def cmd_household(cfg, a, out=sys.stdout, io=None, client_fn=coordinator.client_of, join_fn=coordinator.join):
    """`tutti household status|init|join|leave|code|serve|project|machine` (docs/HOUSEHOLD.md)."""
    io = io or {}
    actions = {'status': lambda: status(cfg, a, out, client_fn), 'init': lambda: init(cfg, a, out, io),
               'join': lambda: join(cfg, a, out, io, join_fn), 'leave': lambda: leave(cfg, out, client_fn),
               'project': lambda: project(cfg, a, out), 'serve': lambda: serve(cfg, a, out),
               'machine': lambda: machine(cfg, a, out, client_fn), 'code': lambda: code(cfg, out, client_fn),
        'job': lambda: hhjobs.run_job(cfg, (a.arg or [''])[0])}
    try:
        return actions[a.action]()
    except (household.HouseholdError, users.UserError) as e:
        say(str(e))
        return 2
    except coordinator.Unreachable as e:
        say(f'{e}: start the dashboard (or `tutti household serve`) on this computer, then try again')
        return 1
    except netpolicy.PublicAddress as e:
        say(f'refused: {e}')
        return 2
