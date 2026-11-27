"""Per-machine read-only GitHub deploy keys (docs/GITHUB.md).

Each household machine gets its own dedicated, read-only SSH deploy key per private or household repository it runs work
for. Because GitHub requires each deploy key to be unique per repository, every (machine, repo) pair uses its own key
under `~/.ssh/tutti-deploy/<slug>`, a managed `Host tutti-gh-<slug>` block in `~/.ssh/config` with `IdentitiesOnly yes`,
and git `url.<alias>.insteadOf` rewrites so `git clone` works automatically and strictly read-only."""
import os
import re
import shlex
import subprocess

from . import github, remote

KEY_DIR = '~/.ssh/tutti-deploy'
TRANSPORTS = ('local', 'ssh', 'wsl')


def slug(repo):
    """A filesystem- and SSH-host-safe slug for an `owner/name` repository."""
    return re.sub(r'[^A-Za-z0-9._-]+', '--', repo.strip()).strip('-')


def host_alias(repo):
    return f'tutti-gh-{slug(repo)}'


def key_title(machine_name, repo):
    return f'tutti:{machine_name}:{repo}'


def _eligible_machines(cfg):
    machines = cfg.get('machines') or {}
    return sorted(name for name, m in machines.items() if isinstance(m, dict) and m.get('transport', 'local') in TRANSPORTS)


def _project_machines(proj, eligible):
    explicit = set(proj.get('machines') or [])
    for suite in (proj.get('tests') or {}).values():
        if isinstance(suite, dict):
            explicit.update(suite.get('machines') or [])
    if not explicit:
        return list(eligible)
    return [m for m in eligible if m in explicit]


def _add_project_repos(plan, proj, eligible):
    if not isinstance(proj, dict):
        return
    repo = str(proj.get('repo') or '').strip()
    if '/' not in repo:
        return
    for m in _project_machines(proj, eligible):
        plan[m].add(repo)


def required_repos(cfg):
    """Map of `{machine_name: [repo, ...]}` for every household machine that runs work for a configured GitHub repo."""
    eligible = _eligible_machines(cfg)
    plan = {m: set() for m in eligible}
    for proj in (cfg.get('projects') or {}).values():
        _add_project_repos(plan, proj, eligible)
    return {m: sorted(repos) for m, repos in plan.items() if repos}


def ssh_config_block(repo, key_file):
    """Managed `~/.ssh/config` entry pinning `repo` to its dedicated read-only deploy key."""
    s = slug(repo)
    return (
        f'# tutti-deploy:begin {s}\n'
        f'Host {host_alias(repo)}\n'
        '    HostName github.com\n'
        '    User git\n'
        f'    IdentityFile {key_file}\n'
        '    IdentitiesOnly yes\n'
        '    StrictHostKeyChecking accept-new\n'
        f'# tutti-deploy:end {s}\n'
    )


def merge_ssh_config(existing, repo, key_file):
    """Idempotently inserts or replaces the managed SSH config block for `repo`."""
    s = slug(repo)
    block = ssh_config_block(repo, key_file)
    pattern = re.compile(rf'# tutti-deploy:begin {re.escape(s)}\n.*?# tutti-deploy:end {re.escape(s)}\n?', re.S)
    if pattern.search(existing):
        return pattern.sub(block, existing, count=1)
    sep = '\n' if existing and not existing.endswith('\n') else ''
    return existing + sep + block


def provision_script(repo):
    """Bash script that creates a machine's per-repo read-only deploy key, configures SSH and git, and prints the pubkey."""
    s = slug(repo)
    alias = host_alias(repo)
    key_rel = f'{KEY_DIR}/{s}'
    q_repo = shlex.quote(repo)
    q_alias = shlex.quote(alias)
    q_slug = shlex.quote(s)
    q_key = shlex.quote(key_rel)
    return (
        'set -eu\n'
        'mkdir -p "$HOME/.ssh/tutti-deploy"\n'
        'chmod 700 "$HOME/.ssh" "$HOME/.ssh/tutti-deploy"\n'
        f'key="$HOME/.ssh/tutti-deploy/"{q_slug}\n'
        'if [ ! -f "$key" ]; then\n'
        f'    ssh-keygen -t ed25519 -N "" -C "tutti-deploy:"{q_repo} -f "$key" >/dev/null\n'
        'fi\n'
        'chmod 600 "$key"\n'
        'cfg="$HOME/.ssh/config"\n'
        'touch "$cfg" && chmod 600 "$cfg"\n'
        f'if ! grep -Fq "# tutti-deploy:begin "{q_slug} "$cfg"; then\n'
        f'    printf "\\n# tutti-deploy:begin %s\\nHost %s\\n    HostName github.com\\n    User git\\n'
        f'    IdentityFile %s\\n    IdentitiesOnly yes\\n    StrictHostKeyChecking accept-new\\n'
        f'# tutti-deploy:end %s\\n" '
        f'{q_slug} {q_alias} {q_key} {q_slug} >> "$cfg"\n'
        'fi\n'
        f'git config --global --replace-all url."git@"{q_alias}":"{q_repo}.insteadOf "git@github.com:"{q_repo}\n'
        f'git config --global --add url."git@"{q_alias}":"{q_repo}.insteadOf "https://github.com/"{q_repo}\n'
        'cat "$key.pub"\n'
    )


def _key_blob(key_text):
    parts = str(key_text or '').strip().split()
    return tuple(parts[:2]) if len(parts) >= 2 else ()


def key_matches(existing_keys, pub_key):
    """True when `pub_key` (type + base64 material) is already registered in `existing_keys`."""
    want = _key_blob(pub_key)
    if not want:
        return False
    return any(_key_blob(item.get('key')) == want for item in existing_keys if isinstance(item, dict))


def _run_remote(machine, script, run=None):
    runner = run or subprocess.run
    argv = remote.argv(machine, script)
    return runner(argv, capture_output=True, text=True, timeout=30, check=False)


def _extract_pubkey(machine_name, repo, res):
    if res.returncode != 0:
        err = (res.stderr or res.stdout or '').strip()[:200]
        raise RuntimeError(f'could not provision deploy key on {machine_name} for {repo}: {err or "ssh failed"}')
    lines = (res.stdout or '').strip().splitlines()
    pub_key = lines[-1].strip() if lines else ''
    if not pub_key.startswith('ssh-'):
        raise ValueError(f'{machine_name} returned an invalid SSH public key for {repo}: check ssh-keygen on {machine_name}')
    return pub_key


def ensure_machine_repo(cfg, machine_name, repo, client=None, run=None):
    """Provisions a read-only deploy key for `(machine_name, repo)` and registers it on GitHub. Returns 'added' or 'present'."""
    machine = (cfg.get('machines') or {}).get(machine_name)
    if not machine:
        raise ValueError(f'machine {machine_name!r} is not in config: add it under machines.<name>')
    pub_key = _extract_pubkey(machine_name, repo, _run_remote(machine, provision_script(repo), run=run))
    c = client or github.client(cfg)
    if key_matches(github.list_deploy_keys(c, repo), pub_key):
        return 'present'
    github.add_deploy_key(c, repo, key_title(machine_name, repo), pub_key)
    return 'added'


def _try_ensure(cfg, machine_name, repo, c, run):
    try:
        state = ensure_machine_repo(cfg, machine_name, repo, client=c, run=run)
        return f'added read-only deploy key for {machine_name} on {repo}' if state == 'added' else ''
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, github.GitHubError):
        return ''


def _sync_machine(cfg, machine_name, repos, c, run):
    lines = [_try_ensure(cfg, machine_name, repo, c, run) for repo in repos]
    return [line for line in lines if line]


def sync(cfg, machines=None, client=None, run=None):
    """Ensures every household machine has read-only deploy keys for its repos. Returns lines describing keys added."""
    if client is None and not github.token_for(cfg)[0]:
        return []
    c = client or github.client(cfg)
    plan = required_repos(cfg)
    wanted = set(machines) if machines is not None else None
    added = []
    for machine_name, repos in plan.items():
        if wanted is None or machine_name in wanted:
            added.extend(_sync_machine(cfg, machine_name, repos, c, run))
    return added


def offline():
    return bool(os.environ.get('TUTTI_OFFLINE'))
