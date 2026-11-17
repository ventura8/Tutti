"""`tutti integrations [linear|slack|discord] [status|setup|test|sync]` (docs/INTEGRATIONS.md).

setup reads the secret (Linear's personal API key, a Slack or Discord webhook URL) from stdin, hidden when typed in a
terminal, checks it and writes it to ~/.config/tutti/integrations.env (mode 600); it never prints it. It does not create
accounts, apps or webhooks: the owner does, with the steps in the doc. test sends one message (or asks Linear who the key
belongs to, printing only that it worked); status says what is on, whether the secret is set, the last round and its error."""
import getpass
import os
import shlex
import sys
import time

from . import config, events, groksetup, linear, secretenv

NAMES = ('linear', 'slack', 'discord')


def save_var(path, name, value):
    """Sets one `export NAME='value'` line in `path` (the other lines kept), created with mode 600."""
    keep = groksetup.read_env_file(path)
    keep[name] = value
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf8') as f:
        f.write('# Tutti: integration secrets (docs/INTEGRATIONS.md); never commit or print this file\n')
        f.writelines(f'export {k}={shlex.quote(v)}\n' for k, v in sorted(keep.items()))
    os.chmod(path, 0o600)


def var_of(cfg, name):
    return linear.settings(cfg)['key_env'] if name == 'linear' else events.settings(cfg, name)['webhook_env']


def read_secret(name, stdin):
    what = 'the Linear personal API key' if name == 'linear' else f'the {name.title()} webhook URL'
    if stdin.isatty():
        return getpass.getpass(f'Paste {what} (hidden): ').strip()
    return stdin.readline().strip()


def enabled(cfg, name):
    return bool(linear.settings(cfg)['enabled'] if name == 'linear' else events.settings(cfg, name)['enabled'])


def status_lines(cfg, env=None, now=None):
    now = now or time.time()
    st_linear = linear.read_state(cfg).get('linear') or {}
    channels = events.load(cfg)['channels']
    out = []
    for name in NAMES:
        var = var_of(cfg, name)
        has = bool(secretenv.get(var, env).strip())
        st = st_linear if name == 'linear' else channels.get(name) or {}
        last = f'last round {int((now - st["last"]) // 60)} min ago' if st.get('last') else 'no round yet'
        line = f'{name}: {"on" if enabled(cfg, name) else "off"}; {var} {"set" if has else "not set"}; {last}'
        if st.get('error'):
            line += f'; error: {st["error"]}'
        if name == 'linear' and st.get('report'):
            line += '; ' + ', '.join(f'{k} {v}' for k, v in st['report'].items())
        out.append(line)
    return out


def run(cfg, a, out=None, stdin=None, env=None, post=None):
    """Returns the exit code. `post` replaces the network in tests (Linear's or the webhook's)."""
    out, stdin = out or sys.stdout, stdin or sys.stdin
    name, action = a.name, a.action or 'status'
    if name in (None, 'status') or action == 'status':
        print('\n'.join(status_lines(cfg, env)), file=out)
        return 0
    if name not in NAMES:
        print(f'integrations: "{name}" is not one of {", ".join(NAMES)}', file=out)
        return 2
    if action == 'setup':
        return setup(cfg, name, out, stdin, env, post)
    if action == 'test':
        return test(cfg, name, out, env, post)
    if action == 'sync' and name == 'linear':
        rep = linear.tick(cfg, client=linear.Client(linear.api_key(cfg, env), post) if post else None, env=env) \
            if enabled(cfg, name) else None
        if rep is None:
            print('integrations linear sync: integrations.linear.enabled is false (or not due): turn it on in Settings or the config', file=out)
            return 1
        print(f'integrations linear sync: {rep.get("error") or ", ".join(f"{k} {len(v)}" for k, v in rep.items())}', file=out)
        return 1 if rep.get('error') else 0
    print(f'integrations {name}: the actions are setup, test, status' + (', sync' if name == 'linear' else ''), file=out)
    return 2


def setup(cfg, name, out, stdin, env, post):
    secret = read_secret(name, stdin)
    if not secret:
        print(f'integrations {name} setup: nothing was given; nothing changed (docs/INTEGRATIONS.md has the steps)', file=out)
        return 1
    if name == 'linear':
        try:
            linear.Client(secret, post).q(linear.VIEWER_Q)
        except linear.LinearError as e:
            print(f'integrations linear setup: the key was not saved: {e}', file=out)
            return 1
    else:
        bad = events.check_url(name, secret)
        if bad:
            print(f'integrations {name} setup: not saved: {bad}', file=out)
            return 1
    var = var_of(cfg, name)
    save_var(os.path.join(config.xdg_tutti_dir(env), secretenv.INTEGRATIONS_FILE), var, secret)
    print(f'integrations {name} setup: {var} saved to ~/.config/tutti/{secretenv.INTEGRATIONS_FILE} (mode 600). '
          f'Turn it on: integrations.{name}.enabled: true (Settings > Integrations, or the config file).', file=out)
    return 0


def test(cfg, name, out, env, post):
    if name == 'linear':
        try:
            linear.Client(linear.api_key(cfg, env), post).q(linear.VIEWER_Q)
        except linear.LinearError as e:
            print(f'integrations linear test: {e}', file=out)
            return 1
        print('integrations linear test: the key works', file=out)
        return 0
    url = secretenv.get(var_of(cfg, name), env).strip()
    if not url:
        print(f'integrations {name} test: {var_of(cfg, name)} is not set: run `tutti integrations {name} setup`', file=out)
        return 1
    why = events.send(name, url, ('Tutti - test: this channel will get Tutti\'s notifications', events.dashboard_url(cfg)), post)
    print(f'integrations {name} test: {why or "sent; look in the channel"}', file=out)
    return 1 if why else 0
