"""`tutti github status|connect|disconnect|manifest` (docs/GITHUB.md): the terminal's Connect GitHub, as the household's
connection (the one the autopilot, `tutti doctor` and the Projects panel use)."""
import html
import json
import sys
import time

from . import github

OWNER = 'owner'   # the dashboard with sign-in off and the terminal share this connection


def say(msg):
    print(f'github: {msg}', file=sys.stderr)


def show(st, out):
    if st['connected']:
        print(f'github: connected as {st["login"] or "?"} (Tutti\'s GitHub App)', file=out)
    elif st['source'] in ('household', 'gh'):
        print(f'github: using the {"household" if st["source"] == "household" else "GitHub CLI"}\'s sign-in; '
              '`tutti github connect` connects your own', file=out)
    else:
        print('github: not connected: run `tutti github connect`, or sign in the GitHub CLI with gh auth login', file=out)


def connect(cfg, out, flows=None, sleep=time.sleep):
    flows = flows or github.Flows(cfg)
    shown = flows.start(OWNER)
    print(f'Open {shown["verification_url"]} on any device and enter the code {shown["user_code"]} '
          f'(it expires in {shown["expires_in"] // 60} min).', file=out)
    while True:
        sleep(shown['interval'])
        login = flows.poll(OWNER)
        if login is not None:
            print(f'github: connected as {login}. Install the App on your repositories: {github.status(cfg)["install_url"]}', file=out)
            return 0


def manifest_page():
    """A page whose one button sends the manifest to GitHub's "create a GitHub App" form (the maintainer, once)."""
    m = html.escape(json.dumps(github.manifest()), quote=True)
    return ('<!doctype html><meta charset="utf-8"><title>Create Tutti\'s GitHub App</title>'
            '<form action="https://github.com/settings/apps/new" method="post">'
            f'<input type="hidden" name="manifest" value="{m}">'
            '<p>This creates Tutti\'s official GitHub App under your account. GitHub shows everything before it is created.</p>'
            '<button type="submit">Create the GitHub App on GitHub</button></form>\n')


def cmd(cfg, a, out=sys.stdout, flows=None, sleep=time.sleep):
    try:
        if a.action == 'connect':
            return connect(cfg, out, flows, sleep)
        if a.action == 'disconnect':
            gone = github.forget(cfg, OWNER)
            print('github: disconnected; revoke it on GitHub too at https://github.com/settings/apps/authorizations' if gone
                  else 'github: nothing was connected here', file=out)
            return 0
        if a.action == 'manifest':
            print(manifest_page(), file=out, end='')
            return 0
        if a.action == 'add-key':
            if len(a.arg) != 3:
                say('github add-key: say repo, title and the public key, e.g. `tutti github add-key owner/name my-machine '
                    '"ssh-ed25519 ..."`')
                return 2
            repo, title, key = a.arg
            c = github.client(cfg, OWNER)
            github.add_deploy_key(c, repo, title, key)
            print(f'github: added read-only deploy key {title} to {repo}', file=out)
            return 0
        show(github.status(cfg, OWNER), out)
        return 0
    except github.GitHubError as e:
        print(f'github: {e}', file=sys.stderr)
        return 1
