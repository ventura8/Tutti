"""Machine hygiene (docs/SELFHEAL.md, *Yield flags, disks and clean-up*): yield flags that expire, a hung WSL restarted,
the free space of every machine's disks (the Windows C: of a WSL machine too) and a clean-up that keeps them roomy.

On 2026-11-16 a yield flag stayed up 15 h on an idle PC, the PC's C: ran full, which hung WSL, and a manual
`docker image prune -a` deleted a project's pinned quality image. So:

- a yield flag carries its owner, reason, pid and expiry (default 4 h, `tutti yield renew` extends it); an expired flag, or
  one whose owner's process is gone while the machine is idle, is lowered and logged;
- a WSL whose Windows side answers ssh but whose `wsl -e true` times out is shut down and started again, never while a yield
  flag or a lease holds the machine;
- under WARN_GB free the machine card warns with the biggest reclaimable items; under STOP_GB routing sends it nothing new
  (tutti/routing.py, the probe's disk field);
- once a day, or under CLEAN_PCT % free, the clean-up: Docker build cache over CACHE_KEEP_GB, dangling images, unused images
  older than IMAGE_DAYS (never an image the config names: `image:` / `images:` anywhere in it, `selfheal.keep_images`),
  Tutti's old runs, logs, worktrees and ~/.cache/tutti-* folders; on Windows old APK outputs (the newest 2 kept), the npm
  and NuGet caches; Remote Desktop traces only on the owner's yes; a WSL disk compacted after a big prune. Never while a
  yield flag or lease holds the machine.

The decisions are pure; every shell call goes through injected runners, so tests use fake machines."""
import base64
import json
import os
import re
import shlex
import socket
import subprocess
import time
from datetime import datetime

from . import config, durable

FLAG_HOURS = 4
WARN_GB, STOP_GB = 15, 8
CLEAN_PCT = 20
CLEAN_EVERY = 24 * 3600
CACHE_KEEP_GB = 20
IMAGE_DAYS = 7
OLD_DAYS = 14
IDLE_RATIO = 0.25          # load under this share of its cores: the machine is idle
COMPACT_GB = 5             # a WSL disk is compacted after a prune that freed at least this much
TRACE_ASK_GB = 2           # Remote Desktop traces at least this big are offered for deletion
STATE = 'hygiene.json'
WIN_C = '/mnt/c'
NONE = '<none>'
WIN_DOWN = 'windows down'
WEEK = 7 * 86400


# ---- yield flags

def flag_text(owner, reason, hours=FLAG_HOURS, pid=None, host=None, now=None):
    """The yield flag's content: one JSON line (a reader that only looks for the word `unload` still finds it)."""
    now = now or time.time()
    return json.dumps({'owner': owner or os.environ.get('USER', ''), 'reason': reason, 'pid': pid, 'host': host or socket.gethostname(),
                       'at': round(now), 'expires': round(now + float(hours) * 3600)})


def parse_flag(text, mtime, now=None):
    """The flag as a dict (owner, reason, pid, host, at, expires, age_s, legacy). A flag written before 0.18 (plain text) is
    owned by nobody known and expires FLAG_HOURS after it was last written."""
    now = now or time.time()
    try:
        d = json.loads(text)
    except ValueError:
        d = None
    if not isinstance(d, dict):
        d = {'owner': '', 'reason': (text or '').strip(), 'pid': None, 'host': '', 'at': mtime,
             'expires': mtime + FLAG_HOURS * 3600, 'legacy': True}
    at = float(d.get('at') or mtime)
    return {'owner': str(d.get('owner') or ''), 'reason': str(d.get('reason') or ''), 'pid': d.get('pid'), 'host': str(d.get('host') or ''),
            'at': at, 'expires': float(d.get('expires') or at + FLAG_HOURS * 3600), 'age_s': max(0.0, now - at),
            'legacy': bool(d.get('legacy'))}


def age_text(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f'{seconds // 60} min'
    return f'{seconds // 3600} h {seconds % 3600 // 60:02d} min'


def renewed(text, mtime, hours=FLAG_HOURS, now=None):
    """The flag's text with its expiry moved to `hours` from now (its owner and reason kept)."""
    now = now or time.time()
    f = parse_flag(text, mtime, now)
    return json.dumps({'owner': f['owner'], 'reason': f['reason'], 'pid': f['pid'], 'host': f['host'], 'at': round(f['at']),
                       'expires': round(now + float(hours) * 3600)})


def stale_flag(flag, now, pid_alive, idle):
    """Why the flag should be lowered, or '': it expired, or its owner's process is gone while the machine is idle."""
    who = f' (owner {flag["owner"]})' if flag['owner'] else ''
    if now >= flag['expires']:
        return f'the yield flag{who} expired: up {age_text(now - flag["at"])}, never renewed'
    if flag.get('pid') and pid_alive is False and idle:
        return f'the yield flag{who} outlived its owner: process {flag["pid"]} is gone and the machine is idle'
    return ''


# ---- one look at a machine: its flag, load and disks

def measure_script(machine):
    from .remote import home_path
    y = home_path(machine.get('yield_file') or '~/.tutti/yield')
    return (f'y={y}; if [ -e "$y" ]; then echo "FLAG $(stat -c %Y "$y" 2>/dev/null || date +%s)"; head -c 2000 "$y" | tr "\\n" " "; echo; '
            'p=$(grep -o \'"pid": *[0-9]*\' "$y" | grep -o "[0-9]*$"); '
            '[ -n "$p" ] && { [ -d "/proc/$p" ] && echo "PID alive" || echo "PID gone"; }; fi; '
            'echo "HOST $(hostname)"; echo "LOAD $(cut -d" " -f1 /proc/loadavg 2>/dev/null || echo 0) $(nproc 2>/dev/null || echo 1)"; '
            'for p in "$HOME" /mnt/c; do [ -d "$p" ] && df -Pk "$p" 2>/dev/null | '
            'awk -v p="$p" \'NR==2{print "DF", p, $2, $4}\'; done; true')


def _flag_line(out, w, lines, i, now):
    out['flag'] = parse_flag(lines[i + 1] if i + 1 < len(lines) else '', float(w[1]), now)


def _df_line(out, w):
    size, free = _num(w[2]) / 1048576, _num(w[3]) / 1048576
    pct = round(100 * free / size, 1) if size else 100.0
    out['disks'][w[1]] = {'size_gb': round(size, 1), 'free_gb': round(free, 1), 'pct': pct}


def parse_measure(text, now=None):
    """{flag, pid_alive, host, load, cores, disks: {path: {size_gb, free_gb, pct}}} from measure_script's output."""
    lines = (text or '').splitlines()
    out = {'flag': None, 'pid_alive': None, 'host': '', 'load': None, 'cores': 1, 'disks': {}}
    for i, ln in enumerate(lines):
        w = ln.split() + ['', '', '']
        tag = w[0]
        if tag == 'FLAG' and w[1]:
            _flag_line(out, w, lines, i, now)
        elif tag == 'PID' and w[1]:
            out['pid_alive'] = w[1] == 'alive'
        elif tag == 'HOST' and w[1]:
            out['host'] = w[1]
        elif tag == 'LOAD' and w[2]:
            out['load'], out['cores'] = _num(w[1]), max(1, int(_num(w[2]) or 1))
        elif tag == 'DF' and w[3]:
            _df_line(out, w)
    return out


def _num(word):
    try:
        return float(word)
    except ValueError:
        return 0.0


def idle(m):
    return m['load'] is not None and m['load'] < IDLE_RATIO * m['cores']


def work_disks(m):
    """The disks work fills: the home disk and a WSL machine's Windows C:."""
    return m['disks']


def disk_level(m):
    """('ok' | 'warn' | 'stop', the lowest free GB, its path, its free %)."""
    disks = work_disks(m)
    if not disks:
        return 'ok', None, '', None
    path, d = min(disks.items(), key=lambda kv: kv[1]['free_gb'])
    level = 'ok'
    if d['free_gb'] < WARN_GB:
        level = 'warn'
    if d['free_gb'] < STOP_GB:
        level = 'stop'
    return level, d['free_gb'], path, d['pct']


def label(path):
    return 'Windows C:' if path == WIN_C else 'its home disk'


# ---- what may never be deleted, and what may

def keep_images(cfg):
    """Every image the config names (`image:` or `images:` anywhere, `selfheal.keep_images`): never deleted."""
    out = set()
    walk_images({k: v for k, v in cfg.items() if not str(k).startswith('_')}, out)
    return sorted(i for i in out if i)


def _names(v):
    if isinstance(v, str):
        return [v]
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def walk_images(x, out):
    if isinstance(x, list):
        for v in x:
            walk_images(v, out)
        return
    if not isinstance(x, dict):
        return
    for k, v in x.items():
        if k in ('image', 'images', 'keep_images'):
            out.update(_names(v))
        walk_images(v, out)


def split_ref(ref):
    """`repo[:tag][@digest]` -> (repo, tag or ''); a registry's port is not a tag."""
    name = ref.split('@')[0]
    if ':' not in name.split('/')[-1]:
        return name, ''
    base, _, tag = name.rpartition(':')
    return base, tag


def _protected(repo, tag, keep):
    return any(repo == base and want in ('', tag) for base, want in map(split_ref, keep))


IMAGES_SCRIPT = ('command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 || exit 0; '
                 'echo IMAGES; docker images --no-trunc --format \'{{.ID}}\t{{.Repository}}\t{{.Tag}}\t{{.CreatedAt}}\'; '
                 'echo USED; docker ps -aq | xargs -r docker inspect --format \'{{.Image}}\' 2>/dev/null; '
                 # when each image was last pulled or tagged here: a base image built long ago but pulled today is not old
                 'echo TAGS; docker images -q --no-trunc | sort -u | xargs -r docker image inspect '
                 '--format \'{{.Id}}\t{{.Metadata.LastTagTime}}\' 2>/dev/null; true')


def parse_created(text):
    """`2026-11-01 10:00:00 +0200 EET` (docker images) or `2026-11-16T10:00:00.123+02:00` (inspect) -> epoch seconds, None
    when unreadable or unset (docker's zero time, year 1)."""
    text = (text or '').strip()
    try:
        if 'T' in text[:11]:
            at = datetime.fromisoformat(re.sub(r'(\.\d{6})\d+', r'\1', text.replace('Z', '+00:00')))
        else:
            at = datetime.strptime(' '.join(text.split()[:3]), '%Y-%m-%d %H:%M:%S %z')
    except ValueError:
        return None
    return at.timestamp() if at.year > 1970 else None


def parse_images(listing):
    """(rows [(id, repo, tag, created)], used ids, {id: last tag time}) from IMAGES_SCRIPT's output."""
    rows, used, tagged, part = [], set(), {}, ''
    for ln in (listing or '').splitlines():
        if ln in ('IMAGES', 'USED', 'TAGS'):
            part = ln
        elif part == 'IMAGES' and ln.count('\t') >= 3:
            rows.append(ln.split('\t')[:4])
        elif part == 'USED' and ln.strip():
            used.add(ln.strip())
        elif part == 'TAGS' and '\t' in ln:
            iid, at = ln.split('\t', 1)
            tagged[iid] = parse_created(at)
    return rows, used, tagged


def images_to_delete(listing, keep, now, days=IMAGE_DAYS):
    """[(ref, why)]: dangling images, and unused ones neither built nor pulled here for `days`; never a kept one or one a
    container (running or stopped) uses. A pinned image pulled by its digest has no tag but a name: it is never dangling."""
    rows, used, tagged = parse_images(listing)
    skip = used | {i for i, r, t, _ in rows if _protected(r, t, keep)}
    out = []
    for iid, repo, tag, created in rows:
        if iid in skip:
            continue
        if repo == NONE and tag == NONE:
            out.append((iid, 'dangling'))
            continue
        ages = [x for x in (parse_created(created), tagged.get(iid)) if x is not None]
        if ages and now - max(ages) > days * 86400:
            out.append((f'{repo}:{tag}' if tag != NONE else iid, f'unused, {int((now - max(ages)) // 86400)} days old'))
    return [x for i, x in enumerate(out) if x not in out[:i]]


def linux_clean_script(refs, cache_gb=CACHE_KEEP_GB, days=IMAGE_DAYS):
    """Frees space on one Linux (or WSL) machine; prints `FREED <kB>` (its home disk, before minus after)."""
    rmi = ' '.join(shlex.quote(r) for r in refs)
    return ('b=$(df -Pk "$HOME" | awk \'NR==2{print $4}\'); '
            'if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then '
            f'docker builder prune -f --keep-storage {int(cache_gb)}gb >/dev/null 2>&1 || '
            f'docker builder prune -f --max-used-space {int(cache_gb)}gb >/dev/null 2>&1; '
            + (f'for r in {rmi}; do docker rmi "$r" >/dev/null 2>&1 && echo "RMI $r"; done; ' if refs else '') +
            'fi; '
            # Tutti's own caches untouched for a week (anything fresh inside keeps the folder)
            'for d in "$HOME"/.cache/tutti-*; do [ -d "$d" ] || continue; '
            f'[ -z "$(find "$d" -newermt "-{int(days)} days" -print -quit 2>/dev/null)" ] && rm -rf -- "$d" && echo "GONE $d"; done; '
            'a=$(df -Pk "$HOME" | awk \'NR==2{print $4}\'); echo "FREED $((a-b))"')


def freed_kb(text):
    for ln in (text or '').splitlines():
        if ln.startswith('FREED '):
            return max(0, int(_num(ln.split()[1])))
    return 0


def old_files(folders, now, days=OLD_DAYS):
    """Tutti's old run and quiet logs: files untouched for `days`. Returns (paths, bytes)."""
    out, size = [], 0
    for folder in folders:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for n in names:
            p = os.path.join(folder, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if os.path.isfile(p) and now - st.st_mtime > days * 86400:
                out.append(p)
                size += st.st_size
    return out, size


def old_worktrees(root, now, busy=(), days=OLD_DAYS):
    """Tutti's run worktrees (tutti-run-*) untouched for `days` and not leased: candidates (checked for changes first)."""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(root, n)
        if not n.startswith('tutti-run-') or not os.path.isdir(p) or in_use(p, busy):
            continue
        marks = [os.path.join(p, x) for x in ('.git', 'HEAD') if os.path.exists(os.path.join(p, x))]
        stamp = max([os.path.getmtime(p), *(os.path.getmtime(x) for x in marks)])
        if now - stamp > days * 86400:
            out.append(p)
    return out


def busy_dirs(proc='/proc'):
    """The working folders of this user's live processes: a worktree one of them works in is never removed."""
    out = set()
    for n in os.listdir(proc) if os.path.isdir(proc) else []:
        if n.isdigit():
            try:
                out.add(os.readlink(os.path.join(proc, n, 'cwd')))
            except OSError:
                continue
    return out


def in_use(path, busy):
    return any(b == path or b.startswith(path + os.sep) for b in busy)


def remove_worktree(path, run=subprocess.run):
    """A clean run worktree removed through git (its branch kept); a worktree with changes is left. Returns True when gone."""
    st = run(['git', '-C', path, 'status', '--porcelain'], capture_output=True, text=True, check=False)
    if st.returncode != 0 or st.stdout.strip():
        return False
    common = run(['git', '-C', path, 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                 capture_output=True, text=True, check=False)
    if common.returncode != 0:
        return False
    r = run(['git', '--git-dir', common.stdout.strip(), 'worktree', 'remove', path], capture_output=True, text=True, check=False)
    return r.returncode == 0


# ---- Windows (a WSL machine's own side), over its plain ssh with PowerShell

def ps(script):
    """A PowerShell command line that survives any remote shell: the script travels base64-encoded."""
    return 'powershell -NoProfile -NonInteractive -EncodedCommand ' + base64.b64encode(script.encode('utf-16-le')).decode()


TRACE_DIR = "Join-Path $env:TEMP 'DiagOutputDir\\RdClientAutoTrace'"
TRACE_SIZE = ps(f'$p={TRACE_DIR}; if (Test-Path $p) {{ (Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue | '
                'Measure-Object Length -Sum).Sum } else { 0 }')
TRACE_DELETE = ps(f'$p={TRACE_DIR}; Get-ChildItem $p -ErrorAction SilentlyContinue | '
                  'Remove-Item -Recurse -Force -ErrorAction SilentlyContinue; '
                  "'done'")


def windows_clean(apk_roots, keep=2):
    """Old APK build outputs (`<project>\\*\\build\\outputs`, the newest `keep` kept), the npm and NuGet caches; prints the
    bytes freed on C: as `FREED <bytes>`."""
    roots = ', '.join("'" + r.replace("'", "''") + "'" for r in apk_roots) or "''"
    return ps('$b=(Get-PSDrive C).Free; '
              f'foreach ($r in @({roots})) {{ if (-not $r -or -not (Test-Path $r)) {{ continue }}; '
              'Get-ChildItem $r -Directory -ErrorAction SilentlyContinue | ForEach-Object { Join-Path $_.FullName \'build\\outputs\' } | '
              'Where-Object { Test-Path $_ } | '
              'ForEach-Object { Get-ChildItem $_ -Recurse -File -Filter *.apk -ErrorAction SilentlyContinue } | '
              f'Sort-Object LastWriteTime -Descending | Select-Object -Skip {int(keep)} | '
              'Remove-Item -Force -ErrorAction SilentlyContinue }; '
              'if (Get-Command npm -ErrorAction SilentlyContinue) { npm cache clean --force *> $null }; '
              'if (Get-Command dotnet -ErrorAction SilentlyContinue) { dotnet nuget locals all --clear *> $null }; '
              '"FREED $((Get-PSDrive C).Free - $b)"')


COMPACT = ps('wsl --shutdown; Start-Sleep 5; '
             '$d = Get-ChildItem HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss | '
             'ForEach-Object { Get-ItemProperty $_.PSPath }; '
             'foreach ($x in $d) { $v = Join-Path $x.BasePath \'ext4.vhdx\'; if (-not (Test-Path $v)) { continue }; '
             'if (Get-Command Optimize-VHD -ErrorAction SilentlyContinue) { try { Optimize-VHD -Path $v -Mode Full -ErrorAction Stop; '
             '"COMPACTED $($x.DistributionName)"; continue } catch {} }; '
             'wsl --manage $x.DistributionName --set-sparse true *> $null; "SPARSE $($x.DistributionName)" }; wsl -e true')


def win_ssh(machine, command, timeout):
    """A command on a WSL machine's Windows side (its plain ssh, as remote.windows_idle). Raises TimeoutExpired."""
    from . import remote
    return subprocess.run([*remote.SSH, *remote.QUICK, machine['host'], command], capture_output=True, text=True, timeout=timeout,
                          check=False)


def wsl_state(win, timeout=30):
    """'ok', 'hung' (Windows answers, `wsl -e true` times out) or 'windows down' (nothing to tell)."""
    try:
        r = win('echo ok', 20)
    except (OSError, subprocess.SubprocessError):
        return WIN_DOWN
    if r.returncode != 0 or 'ok' not in (r.stdout or ''):
        return WIN_DOWN
    try:
        win('wsl -e true', timeout)
    except subprocess.TimeoutExpired:
        return 'hung'
    except (OSError, subprocess.SubprocessError):
        return WIN_DOWN
    return 'ok'


# ---- the state the machine card reads (<state>/hygiene.json)

def state_path(cfg):
    return os.path.join(config.state_dir(cfg), STATE)


def read(cfg):
    try:
        with open(state_path(cfg), encoding='utf8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    return d if isinstance(d, dict) else {}


def write(cfg, data):
    os.makedirs(config.state_dir(cfg), exist_ok=True)
    durable.dump_json(state_path(cfg), data)


def reclaimed_week(entry, now):
    return round(sum(r[1] for r in entry.get('reclaimed') or [] if now - r[0] < WEEK), 1)


def view(cfg, now=None):
    """{machine: {flag, disk, wsl, biggest, reclaimed_week_gb, cleaned_at}}: what the machine cards show."""
    now = now or time.time()
    out = {}
    for name, e in read(cfg).items():
        if isinstance(e, dict):
            out[name] = {k: e.get(k) for k in ('flag', 'disk', 'wsl', 'biggest', 'cleaned_at', 'note')}
            out[name]['reclaimed_week_gb'] = reclaimed_week(e, now)
    return out

