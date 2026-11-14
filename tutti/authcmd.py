"""`tutti auth`: the dashboard's sign-in with Google, set up and recovered from a shell on this computer (docs/AUTH.md).

- `setup [--client-id <id>]`: prints the Google Cloud console page and the exact fields, then reads the client ID (the
  argument, or one line on a terminal). The client ID is public, not a secret; there is no client secret.
- `status`: whether sign-in is on, the origins to authorize, how many users and admins.
- `users`: who signed in, their role and when they were last seen.
- `promote <email>`: make a user admin (shell access on this computer is the proof).
- `reset-admin`: forget every user and session, so the next sign-in becomes admin. Sign in right after it."""
import sys
import time

from . import auth, authweb, config, invites

ACTIONS = ('status', 'setup', 'users', 'promote', 'reset-admin')
CONSOLE = 'https://console.cloud.google.com/apis/credentials/oauthclient'
CONSENT = 'https://console.cloud.google.com/auth/overview'


def say(msg):
    sys.stderr.write(f'tutti: {msg}\n')


def js_origins(cfg):
    """The authorized JavaScript origins to enter in the OAuth client: the loopback names on the dashboard's port, then
    dashboard.origins (a LAN or household address: Google takes only https:// names there, never a bare IP)."""
    port = cfg['dashboard']['port']
    return [f'http://127.0.0.1:{port}', f'http://localhost:{port}', *authweb.origins(cfg)]


def guide(cfg, out):
    lines = [
        'Sign-in with Google: create an OAuth client once (the owner\'s step; Tutti never creates it).',
        '',
        f'1. Consent screen: {CONSENT} (pick or create the project in the top bar first).',
        '   Get started -> App name: Tutti dashboard; User support e-mail: yours; Audience: External;',
        '   Contact e-mail: yours; agree, Create. Then Audience -> Test users -> Add users: every Google account that',
        '   will use the dashboard (Testing mode is enough: no verification is needed for sign-in only).',
        f'2. OAuth client: {CONSOLE}',
        '   (APIs & Services -> Credentials -> Create credentials -> OAuth client ID)',
        '   Application type: Web application',
        '   Name: Tutti dashboard',
        '   Authorized JavaScript origins (add each):',
        *[f'     {o}' for o in js_origins(cfg)],
        '   Authorized redirect URIs: none',
        '   Create, then copy the Client ID (it ends in .apps.googleusercontent.com). No client secret is used.',
        '3. tutti auth setup --client-id <client id>, then restart the dashboard and open it: the first person to sign in',
        '   becomes the administrator.',
    ]
    print('\n'.join(lines), file=out)


def setup(cfg, a, out, io):
    guide(cfg, out)
    value = a.client_id
    if not value and io.get('isatty', lambda: sys.stdin.isatty())():
        value = io.get('input', input)('\nClient ID: ').strip()
    if not value:
        if auth.client_id(cfg):
            print(f'\nsign-in is on (client ID {auth.client_id(cfg)}): nothing changed', file=out)
            return 0
        say('no client ID given: run `tutti auth setup --client-id <id>` once the console shows it')
        return 2
    auth.save_client_id(cfg, value)
    print(f'\nsaved: sign-in with Google is on. Restart the dashboard, open http://127.0.0.1:{cfg["dashboard"]["port"]}/ and sign '
          'in first: you become the administrator.', file=out)
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
    print(f'sign-in: {"on, client ID " + cid if cid else "off (run `tutti auth setup`): the dashboard stays on this computer"}', file=out)
    print(f'origins to authorize: {", ".join(js_origins(cfg))}', file=out)
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
            users.set_role(users.by_email(a.email or ''), 'admin')
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
            print('every user and session is gone: sign in now to become the administrator', file=out)
            return 0
        return status(cfg, out)
    except auth.AuthError as e:
        say(str(e))
        return 2
