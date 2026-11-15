"""Declared needs, installed by themselves (docs/SELFHEAL.md, *Needs*): a suite, a project or a worker lists what it needs
(`needs:`), and before work runs Tutti probes each one on the machine that runs it and installs what is missing, at user
level, pinned and checked: a missing tool is installed, not reported as a failure.

A need is one mapping with one kind key:
- `npm: <pkg>@<exact version>`            npm install -g --prefix ~/.local (npm checks the registry's integrity hash)
- `pip: <pkg>==<version>`, `sha256:`       python3 -m pip install --user (with --require-hashes when sha256 is given)
- `playwright: <browser>`, `version:`      npx playwright@<version> install <browser>
- `ollama: <model>`                        ollama pull
- `docker: <image>@sha256:<digest>`        docker pull by digest
- `dotnet-workload: <id>`                  dotnet workload install
- `bin: <program>`, `download: {url, sha256, unpack: zip|tar|none, path}`   a pinned file, its checksum checked before
  anything runs, unpacked under ~/.local/share/tutti-deps and linked into ~/.local/bin; never piped into a shell
- `apt: <package>` (or `root:` on any need)   needs root: one command for the admin in the needs-input queue

Pure: the probes and installs are one generated bash script; its marker lines say what happened."""
import re
import shlex

KINDS = ('npm', 'pip', 'playwright', 'ollama', 'docker', 'dotnet-workload', 'bin', 'apt')
INSTALLED, ROOT, FAILED = 'TUTTI-DEP-INSTALLED:', 'TUTTI-DEP-ROOT:', 'TUTTI-DEP-FAILED:'
STORE = '$HOME/.local/share/tutti-deps'
EXACT_NPM = re.compile(r'^(@[\w.-]+/)?[\w.-]+@\d+\.\d+\.\d+([-+][\w.]+)?$')
EXACT_PIP = re.compile(r'^[\w.-]+(\[[\w,.-]+\])?==[\w.+-]+$')
DIGEST = re.compile(r'@sha256:[0-9a-f]{64}$')
SHA = re.compile(r'^[0-9a-f]{64}$')


def kind_of(n):
    ks = [k for k in KINDS if k in (n or {})] if isinstance(n, dict) else []
    return ks[0] if len(ks) == 1 else None


def name_of(n):
    return f'{kind_of(n)} {n[kind_of(n)]}'


def need_errors(key, n):
    """Config errors for one need, each with the fix."""
    k = kind_of(n)
    if k is None:
        return [f'{key} is {n!r}: one mapping with one of {", ".join(KINDS)}, e.g. {{npm: typescript@5.6.3}}']
    v = str(n[k])
    rules = {'npm': (EXACT_NPM, 'an exact version, e.g. typescript@5.6.3'), 'pip': (EXACT_PIP, 'an exact version, e.g. ruff==0.8.1'),
             'docker': (DIGEST, 'pinned by digest, e.g. node@sha256:<64 hex>')}
    if k in rules and not rules[k][0].search(v):
        return [f'{key}: {k} {v!r} is not pinned: write {rules[k][1]}']
    if k == 'playwright' and not re.match(r'^\d+\.\d+\.\d+$', str(n.get('version') or '')):
        return [f'{key}: playwright {v} needs version: <the exact Playwright version the project uses, e.g. 1.49.0>']
    if k == 'pip' and n.get('sha256') and not SHA.match(str(n['sha256'])):
        return [f'{key}: sha256 is not 64 hex characters: copy the wheel\'s sha256 from PyPI']
    if k == 'bin' and n.get('download'):
        d = n['download']
        if not isinstance(d, dict) or not str(d.get('url', '')).startswith('https://') or not SHA.match(str(d.get('sha256', ''))):
            return [f'{key}: download needs url: https://... and sha256: <64 hex> (the file\'s checksum from its publisher)']
    if k == 'bin' and not (n.get('download') or n.get('root')):
        return [f'{key}: bin {v} needs download: {{url, sha256}} or root: <the admin\'s install command>']
    return []


def _list(x):
    return x if isinstance(x, list) else []


def _map(x):
    return x if isinstance(x, dict) else {}


def declared(cfg):
    """[(where, need)] from every suite, project and worker (a malformed entry is left to its own config error)."""
    out = []
    for pname, p in _map(cfg.get('projects')).items():
        p = _map(p)
        out += [(f'projects.{pname}.needs[{i}]', n) for i, n in enumerate(_list(p.get('needs')))]
        for sname, s in _map(p.get('tests')).items():
            out += [(f'projects.{pname}.tests.{sname}.needs[{i}]', n) for i, n in enumerate(_list(_map(s).get('needs')))]
    for wname, w in _map(cfg.get('workers')).items():
        out += [(f'workers.{wname}.needs[{i}]', n) for i, n in enumerate(_list(_map(w).get('needs')))]
    return out


def errors(cfg):
    errs = []
    for where, lst in _lists(cfg):
        if not isinstance(lst, list):
            errs.append(f'{where} is {lst!r}: a list of needs, e.g. [{{npm: typescript@5.6.3}}, {{apt: adb}}]')
    return errs + [e for key, n in declared(cfg) for e in need_errors(key, n)]


def _lists(cfg):
    for pname, p in _map(cfg.get('projects')).items():
        p = _map(p)
        if 'needs' in p:
            yield f'projects.{pname}.needs', p['needs'] or []
        for sname, s in _map(p.get('tests')).items():
            if isinstance(s, dict) and 'needs' in s:
                yield f'projects.{pname}.tests.{sname}.needs', s['needs'] or []
    for wname, w in _map(cfg.get('workers')).items():
        if isinstance(w, dict) and 'needs' in w:
            yield f'workers.{wname}.needs', w['needs'] or []


def for_suite(cfg, project, suite):
    p = _map(_map(cfg.get('projects')).get(project))
    return _list(p.get('needs')) + _list(_map(_map(p.get('tests')).get(suite)).get('needs'))


def _q(s):
    return shlex.quote(str(s))


def probe(n):
    """A bash test that succeeds when the need is met."""
    k, v = kind_of(n), str(n[kind_of(n)])
    if k == 'npm':
        pkg, _, ver = v.rpartition('@')
        read = (f"import json,sys; print(json.load(sys.stdin).get('dependencies',{{}}).get({pkg!r},{{}}).get('version',''))")
        return f'[ "$(npm ls -g --prefix "$HOME/.local" --depth=0 --json 2>/dev/null | python3 -c {_q(read)})" = {_q(ver)} ]'
    if k == 'pip':
        pkg, _, ver = v.partition('==')
        pkg = pkg.split('[')[0]
        return f'[ "$(python3 -c "import importlib.metadata as m; print(m.version({pkg!r}))" 2>/dev/null)" = {_q(ver)} ]'
    if k == 'playwright':
        return f'[ -e "{STORE}/"{_q(_pw_stamp(n))} ]'
    return {
        'ollama': f'ollama list 2>/dev/null | awk \'{{print $1}}\' | grep -qxF {_q(v if ":" in v else v + ":latest")}',
        'docker': f'docker image inspect {_q(v)} >/dev/null 2>&1',
        'dotnet-workload': f'dotnet workload list 2>/dev/null | awk \'{{print $1}}\' | grep -qxF {_q(v)}',
        'bin': f'command -v {_q(v)} >/dev/null 2>&1 || [ -x "$HOME/.local/bin/"{_q(v)} ]',
        'apt': f'dpkg -s {_q(v)} >/dev/null 2>&1',
    }[k]


def _pw_stamp(n):
    return f'playwright-{n.get("version")}-{n["playwright"]}.ok'   # written after that version installed that browser


def _download(n):
    d, v = n['download'], str(n['bin'])
    dest, f = f'"{STORE}/"{_q(v)}', f'"{STORE}/"{_q(v)}.dl'
    unpack = {'zip': f'python3 -m zipfile -e {f} {dest}', 'tar': f'tar -xf {f} -C {dest}'}.get(d.get('unpack', 'none'),
                                                                                                 f'cp {f} {dest}/{_q(v)}')
    inner = _q(d.get('path') or v)
    return (f'mkdir -p "{STORE}" "$HOME/.local/bin" && rm -rf {dest} && mkdir -p {dest} && '
            f'curl -fsSL --proto =https -o {f} {_q(d["url"])} && echo {_q(d["sha256"])}"  "{f} | sha256sum -c --quiet - && '
            f'{unpack} && rm -f {f} && chmod +x {dest}/{inner} && ln -sfn {dest}/{inner} "$HOME/.local/bin/"{_q(v)}')


def install(n):
    """The user-level install command, or None when only root can install it."""
    k, v = kind_of(n), str(n[kind_of(n)])
    if k == 'npm':
        return f'npm install -g --prefix "$HOME/.local" --ignore-scripts {_q(v)}'
    if k == 'pip':   # --user; a Python that refuses it (PEP 668) gets the admin's command through `root:`
        if n.get('sha256'):   # --require-hashes: every dependency needs its hash too, so only for a package without any
            return (f'mkdir -p "{STORE}" && printf "%s --hash=sha256:%s\\n" {_q(v)} {_q(n["sha256"])} > "{STORE}/req.txt" && '
                    f'python3 -m pip install --user --require-hashes -r "{STORE}/req.txt"')
        return f'python3 -m pip install --user {_q(v)}'
    if k == 'bin':
        return _download(n) if n.get('download') else None
    if k == 'playwright':
        return f'npx -y playwright@{_q(n.get("version"))} install {_q(v)} && mkdir -p "{STORE}" && touch "{STORE}/"{_q(_pw_stamp(n))}'
    return {'ollama': f'ollama pull {_q(v)}',
            'docker': f'docker pull {_q(v)}', 'dotnet-workload': f'dotnet workload install {_q(v)}', 'apt': None}[k]


def root_command(n):
    k, v = kind_of(n), str(n[kind_of(n)])
    return n.get('root') or (f'sudo apt-get install -y {v}' if k == 'apt' else f'sudo dotnet workload install {v}'
                             if k == 'dotnet-workload' else f'install {v} as root')


def script(needs):
    """A bash script that probes each need and installs the missing ones; exits 1 when one stays missing."""
    lines = ['missing=0', 'export PATH="$HOME/.local/bin:$PATH"']
    for n in needs:
        name, inst = _q(name_of(n)), install(n)
        root = f'echo "{ROOT} "{_q(root_command(n))}; missing=1'
        if inst is None:
            lines.append(f'{{ {probe(n)}; }} || {{ {root}; }}')
            continue
        fallback = root if (n.get('root') or kind_of(n) == 'dotnet-workload') else f'echo "{FAILED} "{name}; missing=1'
        lines.append(f'{{ {probe(n)}; }} || {{ if ( {inst} ) >&2 && {{ {probe(n)}; }}; then echo "{INSTALLED} "{name}; '
                     f'else {fallback}; fi; }}')
    lines.append('[ $missing = 0 ]')
    return '\n'.join(lines) + '\n'


def prefix(needs):
    """The guard a dispatched command runs first: '' when nothing is declared."""
    return f'bash -c {_q(script(needs))} && ' if needs else ''


def parse(text):
    """{installed: [names], root: [commands], failed: [names]} from a script's output."""
    out = {'installed': [], 'root': [], 'failed': []}
    for line in (text or '').splitlines():
        for mark, key in ((INSTALLED, 'installed'), (ROOT, 'root'), (FAILED, 'failed')):
            if line.startswith(mark):
                out[key].append(line[len(mark):].strip())
    return out
