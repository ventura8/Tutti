"""`tutti scout setup grok`: the owner's guided setup of a Grok Bot scout, in a real terminal, with minimum input. It shows
the in-app steps (with the routine's text ready to copy), asks for the webhook URL and the key (hidden), saves both to
~/.config/tutti/grok.env (mode 600), removes an older setup's source line from ~/.profile, adds the scout to the config when missing,
restarts a running dashboard by pid and sends a test question. The key is never echoed, printed or logged.

Tutti reads the file at the point of use (`secretenv.get`), never through an exported variable (backlog t116)."""
import getpass
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from urllib.parse import urlparse

from . import config, scoutcmd, scouts, secretenv
from .review import gh

FILE = 'grok.env'
URL_ENV, KEY_ENV = 'TUTTI_GROK_URL', 'TUTTI_GROK_KEY'
HOST_SUFFIX = 'cursor.sh'          # the routine's "POST to" address is https://api2.cursor.sh/automations/webhook/...
PROC = '/proc'
TEST_QUESTION = "Say hello and name today's date"
COMMENT = "# Tutti: the Grok Bot scout's webhook"
EXPORT_RX = re.compile(r'\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=(.*)')


def env_path(env=None):
    return os.path.join(config.xdg_tutti_dir(env), FILE)


def read_env_file(path):
    """{NAME: value} from `export NAME='value'` lines; {} when the file is absent or unreadable."""
    try:
        with open(path, encoding='utf8') as f:
            lines = f.read().splitlines()
    except OSError:
        return {}
    out = {}
    for line in lines:
        m = EXPORT_RX.fullmatch(line)
        if m:
            try:
                parts = shlex.split(m.group(2))
            except ValueError:
                continue
            out[m.group(1)] = parts[0] if parts else ''
    return out


def check_url(url):
    """None when `url` looks like Grok Bot's webhook, else what is wrong (the URL is not secret, the key is)."""
    u = urlparse(url.strip())
    host = u.hostname or ''
    if u.scheme != 'https' or not host:
        return 'it must start with https:// (copy the routine\'s "POST to" address as it is)'
    if host != HOST_SUFFIX and not host.endswith('.' + HOST_SUFFIX):
        return (f'its host is {host}, not Grok Bot\'s webhook host (…{HOST_SUFFIX}): copy the "POST to" address from the '
                'routine\'s Webhook section')
    return None


def save_env(path, url, key):
    """Writes the two exports, shell-quoted, to `path` with mode 600 (created that way: the key is never world-readable)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf8') as f:
        f.write(f'export {URL_ENV}={shlex.quote(url)}\nexport {KEY_ENV}={shlex.quote(key)}\n')
    os.chmod(path, 0o600)


def profile_line(path):
    home = os.path.expanduser('~')
    shown = '~' + path[len(home):] if path.startswith(home + os.sep) else path
    return f'[ -f {shown} ] && . {shown}'


def remove_from_profile(path, profile=None):
    """Removes the source line (and its comment) an older setup added to ~/.profile: the key must not sit in every shell's
    environment, where an agent printing `env` would show it. True when something was removed."""
    profile = profile or os.path.expanduser('~/.profile')
    try:
        with open(profile, encoding='utf8') as f:
            lines = f.read().splitlines(keepends=True)
    except OSError:
        return False
    keep = [ln for ln in lines if not (path in ln or profile_line(path) in ln or ln.strip() == COMMENT)]
    if len(keep) == len(lines):
        return False
    with open(profile, 'w', encoding='utf8') as f:
        f.write(''.join(keep))
    return True


def repos_of(cfg, name):
    sc = (cfg.get('scouts') or {}).get(name) or {}
    found = [(sc.get('reply') or {}).get('github_repo')]
    found += [(p or {}).get('repo') for p in (cfg.get('projects') or {}).values() if isinstance(p, dict)]
    out = []
    for r in found:
        if scouts.is_repo(r) and r not in out:
            out.append(r)
    return out


def routine_text(repos):
    where = ', '.join(repos)
    return ('Create a routine named Tutti task with a webhook trigger. Instructions: the request body is JSON with task and '
            'reply_to. When kind is "code" (or repo, branch, title, scope and check are set): act as a patch author only — '
            'clone repo, inspect base_sha, and change only scope (do not run builds, push or open a PR). Post ONE comment with a '
            'unified diff in a ```diff fence; Tutti applies it and runs check on its own machines. Otherwise do the task '
            '(research, browse, triage, monitor or draft as it asks) and post the full result as one comment on the GitHub '
            'issue reply_to.url (repository reply_to.repo, issue number reply_to.issue; it is one of '
            f'{where}). Never close the issue. If you cannot do the task, post a comment that says why.')


def steps(repos):
    return [
        'Open the Grok Bot app (Cursor Pro includes it).',
        'Create a new bot named "Tutti scout" and keep it personal: Only me.',
        f'In "Connect apps" (sidebar) connect GitHub and allow it: {", ".join(repos)}.',
        'In the bot\'s chat, paste this text (all of it):',
        'Open the routine (the bot\'s name, Details, the routine), find its Webhook section: keep the "POST to" address and '
        'the key (the part after "Bearer ") at hand for the next two questions.',
    ]


def scout_yaml(name, repo):
    return (f'\nscouts:\n  {name}:\n    kind: grok-bot-webhook\n    url_env: {URL_ENV}\n    key_env: {KEY_ENV}\n'
            f'    reply: {{github_repo: {repo}, issue_label: {scouts.LABEL}}}\n'
            f'    skills: [research, web-browse, triage, monitoring, drafting]\n')


def add_scout(cfg, name, repo):
    """Appends the scout to the config file when it is missing (the file's comments stay); True when added."""
    if name in (cfg.get('scouts') or {}):
        return False
    if cfg.get('scouts'):
        raise scouts.ScoutError(f'the config has scouts: but no "{name}": add it under scouts: in {cfg["_path"]} '
                                f'(docs/SCOUTS.md shows the block), then run this again')
    with open(cfg['_path'], 'a', encoding='utf8') as f:
        f.write(scout_yaml(name, repo))
    return True


def dashboard_pids(proc=PROC):
    """The pids of running `tutti dashboard` servers (not `--once`), read from /proc."""
    pids = []
    for d in os.listdir(proc) if os.path.isdir(proc) else []:
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            with open(os.path.join(proc, d, 'cmdline'), 'rb') as f:
                argv = [a.decode(errors='replace') for a in f.read().split(b'\0') if a]
        except OSError:
            continue
        if 'dashboard' in argv and '--once' not in argv and any(os.path.basename(a) == 'tutti' for a in argv[:3]):
            pids.append(int(d))
    return pids


def restart_dashboard(pid, proc=PROC, kill=os.kill, spawn=subprocess.Popen, sleep=time.sleep, log=None):
    """Stops the dashboard `pid` and starts the same command in the same folder, with this environment minus every secret."""
    with open(os.path.join(proc, str(pid), 'cmdline'), 'rb') as f:
        argv = [a.decode() for a in f.read().split(b'\0') if a]
    cwd = os.readlink(os.path.join(proc, str(pid), 'cwd'))
    kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not os.path.exists(os.path.join(proc, str(pid))):
            break
        sleep(0.1)
    with open(log or os.devnull, 'a', encoding='utf8') as out:   # the child has its own copy of the descriptor
        child = spawn(argv, cwd=cwd, env=secretenv.scrubbed(), stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
    return child.pid


def ask_url(input_fn, out):
    while True:
        url = input_fn('Webhook "POST to" address: ').strip()
        why = check_url(url)
        if not why:
            return url
        print(f'That address will not work: {why}. Try again (Ctrl+C stops).', file=out)


def ask_key(getpass_fn, out):
    while True:
        key = getpass_fn('Webhook key (hidden as you type): ').strip()
        key = key[len('Bearer '):].strip() if key.startswith('Bearer ') else key
        if key:
            return key
        print('The key was empty: paste the part after "Bearer " from the routine\'s Webhook section.', file=out)


def no_tty(name, out):
    print('`tutti scout setup grok` asks for the webhook key with hidden input, so it needs a real terminal (it was started '
          'without one, e.g. from an agent\'s tool). Open a terminal and run:', file=out)
    print(f'\n    tutti scout setup grok{"" if name == "grok" else " " + name}\n', file=out)
    return 2


def run(cfg, name='grok', io=None, gh_fn=gh, post_fn=scouts.post, profile=None, proc=PROC, restart=restart_dashboard):
    """The interactive setup. `io` holds the terminal hooks (isatty, input, getpass, out) so tests drive it."""
    io = io or {}
    out = io.get('out', sys.stdout)
    if not io.get('isatty', lambda: sys.stdin.isatty() and sys.stdout.isatty())():
        return no_tty(name, out)
    input_fn, getpass_fn = io.get('input', input), io.get('getpass', getpass.getpass)
    repos = repos_of(cfg, name)
    while not repos:
        r = input_fn('Which GitHub repository should the answers go to (owner/name)? ').strip()
        repos = [r] if scouts.is_repo(r) else []
    print('Set up the Grok Bot scout (a few minutes, once):\n', file=out)
    for i, s in enumerate(steps(repos), 1):
        print(f'{i}. {s}', file=out)
        if i == 4:
            print(f'\n{routine_text(repos)}\n', file=out)
    input_fn('\nPress Enter when the routine exists and its Webhook section is open... ')
    url, key = ask_url(input_fn, out), ask_key(getpass_fn, out)
    path = env_path()
    save_env(path, url, key)
    print(f'Saved to {path} (only you can read it; Tutti reads it when it asks, never through the environment).', file=out)
    if remove_from_profile(path, profile):
        print('Removed the old line from ~/.profile: the key no longer sits in every shell\'s environment.', file=out)
    if add_scout(cfg, name, repos[0]):
        print(f'Added the scout "{name}" to {cfg["_path"]}.', file=out)
        cfg = config.load(cfg['_path'])
    for pid in dashboard_pids(proc):
        new = restart(pid, proc=proc)
        print(f'Restarted the dashboard (pid {pid} -> {new}).', file=out)
    print(f'Sending a test question: "{TEST_QUESTION}" (the answer can take a few minutes)...', file=out, flush=True)
    a = type('A', (), {'scout': name, 'task': TEST_QUESTION, 'skill': None, 'queue': False, 'repo': None, 'key_stdin': False,
                       'no_wait': False, 'timeout': None})()
    rc = scoutcmd.ask(cfg, a, gh_fn, post_fn)
    print('Done: the scout is set up.' if rc == 0 else
          f'The setup is saved, but the test did not finish (above: why). Try again with: tutti scout ask {name} "{TEST_QUESTION}"',
          file=out)
    return rc
