"""`tutti update`: brings every worker CLI up to date on every machine, without sudo, and names what only the owner can do.

On each machine one bash script runs. For each program the config's CLI workers use (plus `update.programs`) it prints the
version, runs the program's own updater (`cursor-agent update`, `agy update`, `copilot update`, `claude update`), or, for a
CLI installed with npm under a folder the user owns, `npm install -g --prefix <that folder> <package>@latest`, then the
version again. A program that is running at that moment is skipped, so a job never loses its binary mid-run. Packages
installed with apt (`update.packages`, e.g. the Cursor editor or Grok Bot) need sudo: the script reads the installed version,
the newest in the package's own apt repository and whether its source is switched off, and the owner gets one exact command.
Local models (ollama workers) are pulled again through their server, which fetches only changed layers.

The core (the script text, the parsing, the owner's command) is pure; `run` and the dashboard's daily round do the I/O."""
import hashlib
import json
import os
import shlex
import tempfile
import time
import urllib.request

from . import config, inuse, local, remote

STATE_FILE = 'updates-{key}.json'   # one per config: two configs sharing a state folder keep their own rounds
MARK = 'TUTTI-UPD'
# the program's own updater; a program without one is updated through npm when npm installed it under a folder the user owns
SELF_UPDATE = {'cursor-agent': 'update', 'agy': 'update', 'copilot': 'update', 'claude': 'update',
               'codex': 'update'}   # program -> its update subcommand
RETRY_SECONDS = 3600   # a round that failed as a whole is tried again after an hour, not after every_hours
TIMEOUT = 600   # seconds for one program's update on a machine

# Bash, run on each machine. Arguments of `one`: program, version command ('' = `<program> --version`), update command ('' =
# find one), check only (0/1). L: on this computer, the folder of the use locks (tutti/inuse.py); an update runs under
# `flock -n -x` of the program's lock, and a Tutti run holding it shared makes the update wait for the next round.
# Output: MARK<TAB>program<TAB>status<TAB>before<TAB>after<TAB>detail, one line per program or package.
SCRIPT_HEAD = r'''
M=__MARK__
row() { printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$M" "$1" "$2" "$3" "$4" "$5"; }
ver() { if [ -n "$2" ]; then bash -c "$2"; else "$1" --version; fi 2>/dev/null | head -n 1 | tr -d '\t' | cut -c1-120; }
one() {
	p=$1; vc=$2; uc=$3; check=$4
	if ! command -v "$p" > /dev/null 2>&1; then row "$p" missing '' '' 'not installed on this machine'; return; fi
	# in WSL, PATH also holds Windows folders: a Windows copy (an npm shim needing Windows' node) is not this machine's program
	case "$(command -v "$p")" in
		${TUTTI_WIN_PREFIX:-/mnt/[a-z]/}*) row "$p" missing '' '' 'only a Windows copy is on PATH here'; return ;;
	esac
	before=$(ver "$p" "$vc")
	if [ "${p##*/}" = codex ] && [ "$uc" = "$(printf '%q update' "$p")" ] &&
		! "$p" --help 2>/dev/null | grep -Eq '^[[:space:]]*update([[:space:]]|$)'; then
		uc=''   # older Codex releases need the npm installation discovery below
	fi
	if [ -z "$uc" ]; then
		real=$(readlink -f "$(command -v "$p")")
		case "$real" in
			*/lib/node_modules/*)
				prefix=${real%%/lib/node_modules/*}; rest=${real#*/lib/node_modules/}
				case "$rest" in @*) pkg=$(echo "$rest" | cut -d/ -f1-2) ;; *) pkg=${rest%%/*} ;; esac
				uc=$(printf 'npm install -g --prefix %q %q --before=%q' "$prefix" "$pkg@latest" "__BEFORE__")
				if [ ! -w "$prefix/lib/node_modules" ]; then row "$p" owner "$before" '' "sudo $uc"; return; fi ;;
			*) row "$p" manual "$before" '' "no updater known for $real: set workers.<name>.update.command"; return ;;
		esac
	fi
	if [ "$check" = 1 ]; then row "$p" checked "$before" '' "$uc"; return; fi
	b=${p##*/}
	# this script's own shell, and its forks (same command line), carry the program's path: never the program running
	me=$(tr -d '\0' < "/proc/$$/cmdline" 2>/dev/null)
	for x in $(pgrep -f "(^|/)$b([ /.]|$)" 2>/dev/null); do
		c=$(tr -d '\0' < "/proc/$x/cmdline" 2>/dev/null) || continue   # gone already: a short-lived fork of this script
		if [ "$x" != "$$" ] && [ -n "$c" ] && [ "$c" != "$me" ]; then
			row "$p" busy "$before" '' 'running now: the next round updates it'; return
		fi
	done
	if [ -n "$L" ] && command -v flock > /dev/null 2>&1; then
		mkdir -p "$L"
		out=$(timeout __TIMEOUT__ flock -n -E 75 -x "$L/use-$b.lock" bash -c "$uc" 2>&1 < /dev/null); rc=$?
		if [ $rc -eq 75 ]; then row "$p" busy "$before" '' 'a Tutti run is using it: the next round updates it'; return; fi
	else
		out=$(timeout __TIMEOUT__ bash -c "$uc" 2>&1 < /dev/null); rc=$?
	fi
	after=$(ver "$p" "$vc")
	last=$(printf '%s' "$out" | tr -d '\r' | grep -v '^[[:space:]]*$' | tail -n 1 | tr -d '\t' | cut -c1-160)
	if [ $rc -ne 0 ]; then row "$p" failed "$before" "$after" "exit $rc: $last"
	elif [ "$before" != "$after" ]; then row "$p" updated "$before" "$after" "$last"
	else row "$p" current "$before" "$after" "$last"; fi
}
pkg() {
	n=$1
	v=$(dpkg-query -W -f='${Version}' "$n" 2>/dev/null) || v=''
	if [ -z "$v" ]; then row "$n" missing '' '' 'package not installed on this machine'; return; fi
	src=$(grep -l -s -E "^(Suites|URIs):" "/etc/apt/sources.list.d/$n.sources" 2>/dev/null)
	off=''; newest=''
	if [ -n "$src" ]; then
		grep -q -i '^Enabled:[[:space:]]*no' "$src" && off=$src
		uri=$(sed -n 's/^URIs:[[:space:]]*//p' "$src" | head -n 1); suite=$(sed -n 's/^Suites:[[:space:]]*//p' "$src" | head -n 1)
		arch=$(dpkg --print-architecture 2>/dev/null)
		newest=$(curl -fsS --max-time 20 "$uri/dists/$suite/main/binary-$arch/Packages" 2>/dev/null \
			| awk -v n="$n" '/^Package: /{p=($2==n)} p && /^Version: /{print $2}' | sort -V | tail -n 1)
	fi
	if [ -z "$newest" ]; then newest=$(apt-cache policy "$n" 2>/dev/null | sed -n 's/^[[:space:]]*Candidate:[[:space:]]*//p'); fi
	if [ -n "$newest" ] && dpkg --compare-versions "$newest" gt "$v"; then row "$n" package-old "$v" "$newest" "$off"
	else row "$n" package "$v" "$newest" "$off"; fi
}
'''


def programs(cfg):
    """{program: {version, update}} for every program a CLI worker runs, plus `update.programs`; a worker's `update: false`
    leaves its program out, `update: {command, version}` replaces how it is updated or read."""
    out, off = {}, set()
    for w in cfg['workers'].values():
        if w.get('kind') != 'cli':
            continue
        spec = w.get('update', True)
        p = w['command'][0]
        if spec is False:
            off.add(p)   # opted out by any worker that runs it, whatever the order of the workers
            continue
        spec = spec if isinstance(spec, dict) else {}
        out.setdefault(p, {'version': spec.get('version') or '', 'update': spec.get('command') or default_update(p)})
    for p in (cfg.get('update') or {}).get('programs') or []:
        out.setdefault(p, {'version': '', 'update': default_update(p)})
    return {p: s for p, s in out.items() if p not in off}


def default_update(program):
    """The program's own updater, run through the configured executable itself (a path stays that path), else ''."""
    sub = SELF_UPDATE.get(os.path.basename(program))
    return f'{shlex.quote(program)} {sub}' if sub else ''


NPM_COOLDOWN_DAYS = 3   # an npm CLI updates to the newest version that has been out this long (docs/SECURITY-AUDIT-2026-11.md)


def npm_before(now=None):
    """npm's --before date: a version (and every dependency) published in the last NPM_COOLDOWN_DAYS is not installed yet,
    so a hijacked release the registry pulls within days never reaches a machine by itself."""
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=NPM_COOLDOWN_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')


def script(progs, packages, check=False, locks=''):
    """The bash script one machine runs; `locks`: the use-lock folder on this computer (empty on other machines)."""
    head = SCRIPT_HEAD.replace('__MARK__', MARK).replace('__TIMEOUT__', str(TIMEOUT)).replace('__BEFORE__', npm_before())
    lines = [f'L={shlex.quote(locks)}', head]
    for p, s in progs.items():
        lines.append(f'one {shlex.quote(p)} {shlex.quote(s["version"])} {shlex.quote(s["update"])} {1 if check else 0}')
    lines += [f'pkg {shlex.quote(n)}' for n in packages]
    return '\n'.join(lines) + '\n'


def short(text):
    """The version number in a `--version` line ("GitHub Copilot CLI 1.0.112." -> 1.0.112), else the line itself."""
    for word in (text or '').split():
        word = word.strip('.,;()v')
        if word[:1].isdigit() and '.' in word:
            return word
    return (text or '').strip()


def parse(text):
    """The rows a machine's script printed: [{name, status, before, after, detail}]."""
    rows = []
    for line in (text or '').splitlines():
        parts = line.rstrip('\n').split('\t')
        if len(parts) == 6 and parts[0] == MARK:
            _, name, status, before, after, detail = parts
            rows.append({'name': name, 'status': status, 'before': short(before), 'after': short(after), 'detail': detail})
    return rows


def owner_commands(results):
    """What only the owner can run (sudo), one line per machine: a switched-off apt source first, then one apt upgrade of
    every package that has a newer version, then the npm installs that need sudo."""
    out = []
    for machine, r in results.items():
        rows = r.get('items') or []
        old = [x for x in rows if x['status'] == 'package-old']
        off = [x for x in rows if x['status'] in ('package', 'package-old') and x['detail']]   # detail: its switched-off source
        # the same rule as the script's detection: `Enabled:`, any spaces, `no` in any case
        steps = [f"sudo sed -i 's/^Enabled:[[:space:]]*no/Enabled: yes/I' {' '.join(shlex.quote(x['detail']) for x in off)}"] if off else []
        names = list(dict.fromkeys(x['name'] for x in old + off))
        if names:
            steps.append(f'sudo apt update && sudo apt install --only-upgrade {" ".join(names)}')
        steps += [x['detail'] for x in rows if x['status'] == 'owner']
        if steps:
            why = [f'{x["name"]} {x["before"]} -> {x["after"]}' for x in old]
            why += [f'{x["name"]}: its apt source is switched off, so apt never sees new versions' for x in off]
            why += [f'{x["name"]}: npm installed it where only root can write' for x in rows if x['status'] == 'owner']
            out.append({'machine': machine, 'why': '; '.join(why), 'command': ' && '.join(steps)})
    return out


def _ollama(host, path, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(config.ollama_url(host, path), data, {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b'{}')


def _models(host, path, call):
    """The model list of /api/tags or /api/ps; ValueError when the answer is not one."""
    data = call(host, path)
    models = data.get('models') if isinstance(data, dict) else None
    if not isinstance(models, list) or not all(isinstance(m, dict) for m in models):
        raise ValueError(f'{path} did not answer with a model list')
    return models


def _named(model):
    return {model, model if ':' in model else model + ':latest'}


def _digest(host, model, call):
    return next((str(m.get('digest', ''))[:12] for m in _models(host, '/api/tags', call) if m.get('name') in _named(model)), '')


def _model_row(cfg, name, w, check, call):
    label = f'{w["model"]} ({name})'
    machine = cfg['machines'].get(w.get('machine') or '')
    if machine and not check and local.yield_cached(machine) is not None:
        return {'name': label, 'status': 'busy', 'before': '', 'after': '',
                'detail': f'{machine["name"]} is lent out (its yield flag is up): the next round pulls it'}
    try:
        before = _digest(w['host'], w['model'], call)
        if check:
            return {'name': label, 'status': 'checked', 'before': before, 'after': '', 'detail': w['host']}
        if any(m.get('name') in _named(w['model']) for m in _models(w['host'], '/api/ps', call)):
            return {'name': label, 'status': 'busy', 'before': before, 'after': '',
                    'detail': 'loaded in its server now: the next round pulls it'}
        call(w['host'], '/api/pull', {'model': w['model'], 'stream': False}, 3600)
        after = _digest(w['host'], w['model'], call)
        return {'name': label, 'status': 'updated' if after != before else 'current', 'before': before, 'after': after, 'detail': w['host']}
    except (OSError, ValueError) as e:
        return {'name': label, 'status': 'failed', 'before': '', 'after': '', 'detail':
                f'{w["host"]} did not answer as a model server ({e}): start it or its tunnel, then run tutti update again'}


def pull_models(cfg, check=False, call=_ollama, only=None):
    """Each ollama worker's model pulled again through its own server (with `only`, the workers bound to that machine), unless
    its machine is lent out or the model is loaded now: [{name, status, before, after, detail}]."""
    return [_model_row(cfg, name, w, check, call) for name, w in cfg['workers'].items()
            if w.get('kind') == 'ollama' and w.get('update', True) is not False and (not only or w.get('machine') == only)]


def run_machine(machine, text, sh=remote.sh):
    """{items, error} for one machine."""
    try:
        r = sh(machine, text, capture=True, timeout=TIMEOUT * 4)
    except (OSError, ValueError) as e:
        return {'items': [], 'error': f'could not start the update there ({e})'}
    except Exception as e:   # subprocess.TimeoutExpired and the like: the other machines still go ahead
        return {'items': [], 'error': f'the update did not finish ({type(e).__name__}): run tutti update --machine {machine["name"]}'}
    rows = parse(r.stdout)
    if r.returncode != 0:
        first = ((r.stderr or '').strip().splitlines() or ['no output'])[0][:160]
        what = 'stopped part way' if rows else 'unreachable or the script failed'
        return {'items': rows, 'error': f'{what} ("{first}", exit {r.returncode}): check `tutti machines`, then tutti update again'}
    return {'items': rows, 'error': None}


class Busy(Exception):
    """Another update round (a dashboard's or `tutti update`) is running for this config."""


def run(cfg, check=False, only=None, sh=remote.sh, call=_ollama, clock=time.time, when_due=False):
    """Updates (or with check, only reads) every machine, one round at a time per config (Busy otherwise); returns {at, check,
    machines, models, owner}. A full round is saved; a round of one machine is merged into the last full one, which keeps its
    time. `when_due`: the dashboard's round, which checks again under the lock that it is still due (None when not)."""
    with inuse.exclusive(path(cfg) + '.lock') as mine:
        if not mine:
            raise Busy('another update round is running for this config: wait for it (the dashboard shows when it is done)')
        if when_due and not due(cfg, clock(), load(cfg)):
            return None
        try:
            return _round(cfg, check, only, sh, call, clock)
        except Exception as e:
            if not check:   # under the same lock, so no other round's results are overwritten
                save(cfg, {**(load(cfg) or {'at': None, 'machines': {}, 'models': [], 'owner': []}), 'failed_at': clock(),
                           'error': f'the update round failed ({type(e).__name__}: {e}): run tutti update to see why'})
            raise


def lent_out(machine):
    """The yield note of a machine that is lent out, else None (an unreachable machine is not lent out: its round says so)."""
    try:
        return local.yield_cached(machine)
    except (OSError, ValueError):
        return None


def _machine_round(cfg, m, progs, packages, check, sh, last):
    if not check and lent_out(m) is not None:
        kept = ((last or {}).get('machines') or {}).get(m['name']) or {}
        return {'items': kept.get('items') or [], 'error': None,
                'note': 'lent out (its yield flag is up): not updated this round, the versions are from the last one'}
    locks = inuse.lock_dir(config.state_dir(cfg)) if m['transport'] == 'local' else ''
    return run_machine(m, script(progs, packages, check, locks), sh)


def _round(cfg, check, only, sh, call, clock):
    u = cfg.get('update') or {}
    progs, packages, last = programs(cfg), list(u.get('packages') or []), load(cfg)
    results = {n: _machine_round(cfg, m, progs, packages, check, sh, last) for n, m in cfg['machines'].items() if not only or n == only}
    out = {'at': clock(), 'check': check, 'machines': results, 'models': pull_models(cfg, check, call, only),
           'owner': owner_commands(results)}
    if check:
        return out
    if only and last:
        machines = {**(last.get('machines') or {}), **results}
        models = [x for x in last.get('models') or [] if x['name'] not in {m['name'] for m in out['models']}] + out['models']
        save(cfg, {**last, 'machines': machines, 'models': models, 'owner': owner_commands(machines)})
    elif not only:
        save(cfg, out)
    return out


def path(cfg):
    key = hashlib.sha256(os.path.abspath(cfg.get('_path') or 'tutti').encode()).hexdigest()[:10]
    return os.path.join(config.state_dir(cfg), STATE_FILE.format(key=key))


def save(cfg, data):
    p = path(cfg)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix='.updates-', suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.replace(tmp, p)


def load(cfg):
    try:
        with open(path(cfg), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def due(cfg, now, last):
    """True when the dashboard's automatic round should run: `update.auto` is on and the last round is older than
    `update.every_hours` (or there was none)."""
    u = cfg.get('update') or {}
    if not u.get('auto', True):
        return False
    if not last:
        return True
    failed = float(last.get('failed_at') or 0)
    if failed > float(last.get('at') or 0):   # the newest round failed as a whole: tried again after RETRY_SECONDS
        return now - failed >= RETRY_SECONDS
    return now - float(last.get('at') or 0) >= float(u.get('every_hours', 24)) * 3600


def view(cfg, data):
    """What the dashboard shows: the last round, flat, plus whether the automatic round is on."""
    u = cfg.get('update') or {}
    base = {'auto': bool(u.get('auto', True)), 'every_hours': u.get('every_hours', 24)}
    if not data:
        return {**base, 'at': None, 'machines': [], 'models': [], 'owner': []}
    machines = [{'name': n, 'error': r.get('error'), 'note': r.get('note'), 'items': r.get('items') or []}
                for n, r in (data.get('machines') or {}).items()]
    return {**base, 'at': data.get('at'), 'machines': machines, 'models': data.get('models') or [], 'owner': data.get('owner') or []}


STATUS_TEXT = {'updated': 'updated', 'current': 'up to date', 'busy': 'skipped', 'failed': 'FAILED', 'missing': 'not installed',
               'owner': 'needs the owner', 'manual': 'no updater', 'checked': 'installed', 'package': 'package, newest',
               'package-old': 'package, OLD'}


def machine_lines(name, r):
    lines = [f'{name}:' + ''.join(f' {t}' for t in (r.get('error'), r.get('note')) if t)]
    for x in r['items']:
        if x['status'] == 'missing':
            continue
        ver = x['before'] + (f' -> {x["after"]}' if x['after'] and x['after'] != x['before'] else '')
        detail = x['detail'] if x['status'] in ('failed', 'busy', 'manual') else ''
        lines.append(f'  {x["name"]:14} {ver:28} {STATUS_TEXT.get(x["status"], x["status"])}  {detail}'.rstrip())
    return lines


def report(data):
    """Plain text for `tutti update`."""
    lines = [line for n, r in data['machines'].items() for line in machine_lines(n, r)]
    for x in data['models']:
        detail = x['detail'] if x['status'] in ('failed', 'busy') else ''
        lines.append(f'model {x["name"]}: {STATUS_TEXT.get(x["status"], x["status"])} {detail}'.rstrip())
    lines += [f'owner, on {o["machine"]} ({o["why"]}): {o["command"]}' for o in data['owner']]
    return '\n'.join(lines)
