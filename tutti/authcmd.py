"""`tutti auth`: the dashboard's sign-in with Google, set up and recovered from a shell on this computer (docs/AUTH.md).

- `setup [--client-id <id>]`: prints the Google Cloud console page and the exact fields, then reads the client ID (the
  argument, or one line on a terminal). The client ID is public, not a secret; there is no client secret.
- `status`: whether sign-in is on, the origins to authorize, how many users and admins.
- `users`: who signed in, their role and when they were last seen.
- `promote <email>`: make a user admin (shell access on this computer is the proof).
- `reset-admin`: forget every user and session, and revoke every invite, so the next sign-in becomes admin. Sign in right after it."""
import sys
import time

from . import auth, authdevice, authweb, config, invites, official

ACTIONS = ('status', 'setup', 'users', 'promote', 'reset-admin')
CONSOLE = 'https://console.cloud.google.com/apis/credentials/oauthclient'
CONSENT = 'https://console.cloud.google.com/auth/overview'


def say(msg):
    sys.stderr.write(f'tutti: {msg}\n')


def js_origins(cfg):
    """The authorized JavaScript origins to enter in the OAuth client (auth.required_origins)."""
    return auth.required_origins(cfg)


def reach(cfg, ts_name):
    """How other devices reach the dashboard, as guide lines: the Tailscale https name when there is one, else localhost only."""
    port = cfg['dashboard']['port']
    if any(o.endswith('.ts.net') for o in authweb.origins(cfg)):
        return ['   Other devices: through the Tailscale https name above (tailnet only, never the internet).']
    if ts_name:
        return [f'   Other devices: Tailscale is here ({ts_name}). Run `tutti auth setup --tailscale`: it runs',
                f'   `tailscale serve --bg --https=443 http://127.0.0.1:{port}` (a real certificate, tailnet only) and adds',
                f'   https://{ts_name} to the list above; add it to the OAuth client too.']
    return ['   Other devices: install Tailscale on this computer and theirs, then run `tutti auth setup --tailscale`.',
            '   Google takes plain http only for localhost, so a LAN or VPN address needs a private https name;',
            '   until then the dashboard is for this computer only.']


def lan_addresses():
    """This computer's LAN and VPN addresses (tutti/netpolicy.py), for the address others open."""
    from . import netpolicy
    return netpolicy.private_addresses(netpolicy.interfaces())


def guide(cfg, out, ts_name='', lan=()):
    port = cfg['dashboard']['port']
    where = f'{authweb.LOCAL_SCHEME}://{lan[0]}:{port}/' if lan else f'this computer\'s LAN address on port {port}'
    lines = [
        'Sign-in with Google: two OAuth clients, created once in one Google Cloud project (the owner\'s step; Tutti never',
        'creates them). Anyone on the home network, also through a VPN, then signs in with Google; nothing else to set.',
        '',
        f'1. Consent screen: {CONSENT} (pick or create the project in the top bar first).',
        '   Get started -> App name: Tutti dashboard; User support e-mail: yours; Audience: External;',
        '   Contact e-mail: yours; agree, Create. Then Audience -> Test users -> Add users: every Google account that',
        '   will use the dashboard (Testing mode is enough: no verification is needed for sign-in only).',
        f'2. OAuth client for the LAN and any VPN: {CONSOLE}',
        '   Application type: TVs and Limited Input devices',
        '   Name: Tutti dashboard (LAN)',
        '   Create, then copy the Client ID and the Client secret (Google requires the secret for this flow; Tutti keeps',
        '   it mode 600 in the state folder and sends it only to Google).',
        f'   It needs no origin and no https: a browser at {where} shows a code to enter at google.com/device.',
        f'3. OAuth client for this computer: {CONSOLE}',
        '   Application type: Web application',
        '   Name: Tutti dashboard',
        '   Authorized JavaScript origins (add each):',
        *[f'     {o}' for o in js_origins(cfg)],
        *reach(cfg, ts_name),
        '   Authorized redirect URIs: none',
        '   Create, then copy the Client ID. This client has no secret to keep.',
        '4. Save both (no prompt; the secret through stdin, never on the command line):',
        "   printf '%s' '<secret>' | tutti auth setup --client-id <web id> --device-client-id <tv id> --device-secret-stdin",
        f'5. Restart the dashboard and open it (http://localhost:{port}/ here, {where} elsewhere): the first person to sign',
        '   in becomes the administrator.',
    ]
    print('\n'.join(lines), file=out)


def serve_tailscale(cfg, ts_name, out, run=None):
    """`tailscale serve` in front of the loopback dashboard (https on the tailnet only; never `funnel`), then the origin saved."""
    import subprocess
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=30))
    cmd = ['tailscale', 'serve', '--bg', '--https=443', f'http://127.0.0.1:{cfg["dashboard"]["port"]}']
    try:
        res = run(cmd)
        ok, why = res.returncode == 0, (res.stderr or res.stdout or '').strip()[:300]
    except (OSError, subprocess.SubprocessError) as e:
        ok, why = False, str(e)
    if not ok:
        say(f'`{" ".join(cmd)}` failed ({why}): if it asks for permission, run `sudo tailscale set --operator=$USER` once, '
            'then `tutti auth setup --tailscale` again')
        return False
    auth.save_origin(cfg, f'https://{ts_name}')
    print(f'\nserved: https://{ts_name}/ reaches the dashboard inside the tailnet; add that origin to the OAuth client', file=out)
    return True


def builtin_only(cfg, a):
    """True when Tutti's own clients already cover sign-in and nothing was asked for: no console steps to print."""
    asked = a.client_id or getattr(a, 'device_client_id', None) or getattr(a, 'tailscale', False)
    return not asked and official.on(cfg) and auth.enabled(cfg)


def setup(cfg, a, out, io):
    if builtin_only(cfg, a):
        lan = 'and from the LAN or a VPN (a code at google.com/device)' if authdevice.enabled(cfg) else \
            '(from the LAN or a VPN this release has no built-in code sign-in: add your own device client, docs/AUTH.md)'
        print(f'sign-in with Google is built in: open the dashboard on this computer {lan}, choose Sign in with Google, '
              'and the first person to sign in becomes the administrator. Nothing to create in Google Cloud.', file=out)
        return 0
    ts_name = auth.tailscale_name()
    if getattr(a, 'tailscale', False):
        if not ts_name:
            say('Tailscale is not installed or not signed in here: install it (https://tailscale.com/download), run '
                '`tailscale up`, then `tutti auth setup --tailscale` again')
            return 2
        if a.client_id:
            auth.save_client_id(cfg, a.client_id)
        if not auth.client_id(cfg):
            say('set the client ID first (`tutti auth setup --client-id <id>`): the tailnet name is served only with sign-in on')
            return 2
        if not serve_tailscale(cfg, ts_name, out):
            return 2
    guide(cfg, out, ts_name, io.get('lan', lan_addresses)())
    for problem in auth.network_errors(cfg):
        print(f'\nnot allowed: {problem}', file=out)
    return save_clients(cfg, a, out, io)


def save_clients(cfg, a, out, io):
    """Saves what was given (any of the two clients); asks on a terminal only for what is missing; 0 when sign-in is on."""
    tty = io.get('isatty', lambda: sys.stdin.isatty())()
    web, tv = a.client_id, getattr(a, 'device_client_id', None)
    secret = io.get('stdin', sys.stdin).read().strip() if getattr(a, 'device_secret_stdin', False) else ''
    if tty and not (web or tv):
        web = io.get('input', input)('\nWeb application client ID (Enter to skip): ').strip()
        tv = io.get('input', input)('TVs and Limited Input client ID (Enter to skip): ').strip()
    if tv and not secret and tty:
        import getpass
        secret = io.get('getpass', getpass.getpass)('Its client secret (not shown): ').strip()
    if web:
        auth.save_client_id(cfg, web)
    if tv:
        authdevice.save(cfg, tv, secret)
    if not auth.enabled(cfg):
        say('no client given: run `tutti auth setup --client-id <web id> --device-client-id <tv id> --device-secret-stdin` once '
            'the console shows them')
        return 2
    have = [n for n, on in (('this computer (Google\'s button)', auth.client_id(cfg)),
                            ('the LAN and any VPN (a code at google.com/device)', authdevice.enabled(cfg))) if on]
    print(f'\nsign-in with Google is on for {" and ".join(have)}. Restart the dashboard and sign in first: you become the '
          'administrator.', file=out)
    return 0


def print_users(cfg, out, now):
    users = auth.Users(config.state_dir(cfg)).all()
    if not users:
        print('nobody has signed in yet: the first sign-in becomes admin', file=out)
    for u in sorted(users.values(), key=lambda u: (u.get('role') != 'admin', u.get('email') or '')):
        seen = int((now - (u.get('last_seen') or now)) / 60)
        print(f'  {u.get("role", "?"):8} {u.get("email") or "?":40} last seen {seen} min ago', file=out)
    return 0


def status(cfg, out):
    folder = config.state_dir(cfg)
    cid = auth.client_id(cfg)
    users = auth.Users(folder).all()
    admins = sum(1 for u in users.values() if u.get('role') == 'admin')
    live = sum(1 for i in invites.Invites(folder).listing() if i['live'])
    tv = authdevice.client(cfg)[0]
    print(f'sign-in: {"on" if auth.enabled(cfg) else "off (run `tutti auth setup`): the dashboard stays on this computer"}; '
          f'button client {cid or "(none)"}; LAN/VPN device client {tv or "(none)"}', file=out)
    if official.on(cfg):
        print("Tutti's own sign-in clients are built into this release; a client of the household's own wins", file=out)
    print(f'origins to authorize: {", ".join(js_origins(cfg))}', file=out)
    for problem in auth.network_errors(cfg):
        print(f'not allowed: {problem}', file=out)
    print(f'users: {len(users)} ({admins} admin); live invites: {live}', file=out)
    return 0


def cmd_auth(cfg, a, out=sys.stdout, io=None):
    io = io or {}
    try:
        if a.action == 'setup':
            return setup(cfg, a, out, io)
        if a.action == 'users':
            return print_users(cfg, out, time.time())
        if a.action == 'promote':
            users = auth.Users(config.state_dir(cfg))
            sub = users.by_email(a.email or '')
            users.set_role(sub, 'admin')
            auth.audit(config.state_dir(cfg), {'sub': 'shell', 'email': 'tutti auth promote'}, 'role', users.get(sub), 'admin')
            print(f'{auth.redact_email(a.email)} is admin now', file=out)
            return 0
        if a.action == 'reset-admin':
            if not a.yes:
                say('reset-admin forgets every user and session; the next person to sign in becomes admin. Run it again with '
                    '--yes, then sign in at once')
                return 2
            folder = config.state_dir(cfg)
            auth.Users(folder).reset()
            auth.Sessions(folder, 1).clear()
            invites.Invites(folder).revoke_all()
            print('every user, session and invite is gone: sign in now to become the administrator', file=out)
            return 0
        return status(cfg, out)
    except auth.AuthError as e:
        say(str(e))
        return 2
