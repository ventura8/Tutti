"""`tutti files`: copies files and folders to machines over their own transports (ssh, WSL, this computer). rsync when both
ends have it (resumable partials, skips what is already there), else a resumable byte stream; every item is checked with
sha256sum on the machine before it counts as stored. Items sent without --dest live in the machine's cache, which keeps
to a size budget by removing the least recently used items first. Secrets never leave: the send is refused instead."""
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager

from . import config, files, homeassistant, remote

try:
    import fcntl
except ImportError:   # Windows: no flock; msvcrt's byte lock instead
    fcntl = None


def lock_file(f, sleep=time.sleep):
    """An exclusive lock on an open file until it closes: flock, or on Windows msvcrt.locking (retried while held)."""
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_EX)
        return
    import msvcrt
    while True:
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            sleep(1)

CHUNK = files.MB
SSH_OPTS = ('-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes')


def log(msg):
    sys.stderr.write(f'tutti: {msg}\n')


# ---- paths on the machine


def base_of(cfg, machine, dest=None):
    return dest or files.settings(cfg, machine)['cache']


def shell_path(base, rel=''):
    """A path for a bash line on the machine: ~ becomes $HOME, the rest quoted."""
    return remote.home_path(base) + (f'/{shlex.quote(rel)}' if rel else '')


def rsync_target(machine, base):
    """The rsync destination: a local path for this computer, else host:path with ~/ made relative to the login's home."""
    if machine['transport'] == 'local':
        return os.path.expanduser(base).rstrip('/') + '/'
    rel = base[2:] if base.startswith('~/') else ('.' if base == '~' else base)
    return f'{remote.dest(machine)}:{rel.rstrip("/")}/'


# ---- the job record the dashboard reads (state/files/<started>-<pid>.log, .cmd, .progress, .rc)


class Job:
    def __init__(self, cfg, title, folder='files', target='', spec=None):
        self.cfg = cfg
        folder = os.path.join(config.state_dir(cfg), folder)
        os.makedirs(folder, exist_ok=True)
        self.log_path = os.path.join(folder, f'{int(time.time())}-{os.getpid()}-{time.monotonic_ns() % 100000}.log')
        with open(self.log_path + '.cmd', 'w', encoding='utf8') as f:
            f.write(title + '\n')
        if target:   # what a "pause, stop now" of a machine, device or worker stops (tutti/controls.py), and how to hand it over
            for ext, text in (('.target', target), ('.pid', str(os.getpid())), ('.job', json.dumps(spec or {}))):
                with open(self.log_path + ext, 'w', encoding='utf8') as f:
                    f.write(text + '\n')
        self.log = open(self.log_path, 'a', encoding='utf8')   # closed by end()
        self.total = 0
        self.base = 0

    def say(self, msg):
        self.log.write(msg + '\n')
        self.log.flush()
        log(msg)

    def progress(self, done):
        with open(self.log_path + '.progress', 'w', encoding='utf8') as f:
            f.write(files.progress_line(self.base + done, self.total))
        os.utime(self.log_path)   # bytes moving is activity: a long copy is not "stalled" because its log is quiet

    def finish(self):
        with open(self.log_path + '.progress', 'w', encoding='utf8') as f:
            f.write(files.progress_line(self.total, self.total))

    def end(self, rc):
        with open(self.log_path + '.rc', 'w', encoding='utf8') as f:
            f.write(f'{rc}\n')
        self.log.close()
        homeassistant.notify(self.cfg, f'Tutti job finished (exit {rc})')


def lock_dir(env=None):
    """Shared by every project on this computer (not the project's state folder): $XDG_STATE_HOME/tutti/locks."""
    env = os.environ if env is None else env
    return os.path.join(os.path.expanduser(env.get('XDG_STATE_HOME') or '~/.local/state'), 'tutti', 'locks')


@contextmanager
def machine_lock(machine, base):
    """One send or clean at a time per destination folder from this computer, whichever project or config asks: two sends
    of the same archive would fight over its partial file, and a clean must not delete what a send is verifying. Keyed by
    the host and the folder, so two projects with the same cache share the lock."""
    os.makedirs(lock_dir(), exist_ok=True)
    key = hashlib.sha256(f'{machine.get("host") or "local"}|{base}'.encode()).hexdigest()[:16]
    with open(os.path.join(lock_dir(), f'files-{key}.lock'), 'w', encoding='utf8') as f:
        lock_file(f)
        yield


# ---- what the machine has


def inspect_script(base):
    b = shell_path(base)
    return (f'mkdir -p {b}/{files.META} && cd {b} || exit 3; '
            'command -v rsync > /dev/null && echo RSYNC=1 || echo RSYNC=0; '
            'echo "FREE=$(df -PB1 . | awk \'NR==2{print $4}\')"; '
            f'echo "PARTIAL=$(find . \\( -name \'*{files.PART_SUFFIX}\' -o -path \'*/{files.PARTIAL}/*\' \\) -type f -printf \'%s\\n\' '
            '| awk \'{s+=$1} END {print s+0}\')"; '
            f'find {files.META} -name \'*.pin-*\' -mmin +{files.PIN_HOURS * 60} -delete; '
            f'for p in {files.META}/*.pin-*; do [ -e "$p" ] || continue; p=${{p#{files.META}/}}; '
            'printf "PIN\\t%s\\n" "${p%.pin-*}"; done; '
            f'for f in {files.META}/*.json; do [ -e "$f" ] || continue; n=${{f#{files.META}/}}; n=${{n%.json}}; '
            f'if [ -e "$n" ]; then printf "ITEM\\t%s\\t%s\\t%s\\n" "$n" "$(stat -c %Y "$f")" "$(tr -d "\\n" < "$f")"; '
            f'else rm -f "$f" "{files.META}/$n.sha256"; fi; done')


def inspect(machine, base, timeout=60):
    """{rsync, free, items: {name: (Entry, meta)}} for the folder on the machine (created when missing)."""
    try:
        r = remote.sh(machine, inspect_script(base), capture=True, timeout=timeout, ssh_opts=SSH_OPTS)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f'{machine["name"]} did not answer within {timeout} s: check it is on (`tutti machines`), '
                           'then try again') from e
    if r.returncode == 255 and machine['transport'] != 'local':
        raise RuntimeError(f'{machine["name"]} cannot be reached over ssh ({machine.get("host")}): check it is on and `ssh '
                           f'{machine.get("host")} true` works without a password, then try again')
    if r.returncode != 0:
        raise RuntimeError(f'{machine["name"]}: could not create or open {base} (exit {r.returncode}): check the path and that '
                           f'its disk is writable; the cache is files.cache in the config')
    out = r.stdout
    return {'rsync': 'RSYNC=1' in out, 'free': files.parse_number(out, 'FREE'), 'partial': files.parse_number(out, 'PARTIAL') or 0,
            'items': files.parse_listing(out), 'pins': files.parse_pins(out)}


def stored(cfg, machine, timeout=30):
    """The dashboard's view of the machine's cache: its items, their size and last use, the budget. Never raises."""
    s = files.settings(cfg, machine)
    out = {'cache': s['cache'], 'budget_bytes': int(s['budget_gb'] * files.GB), 'items': [], 'bytes': 0, 'problem': None}
    try:
        info = inspect(machine, s['cache'], timeout=timeout)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        out['problem'] = str(e)
        return out
    items = sorted((e for e, _ in info['items'].values()), key=lambda e: -e.used_at)
    out['items'] = [{'name': e.name, 'bytes': e.bytes, 'used_at': e.used_at, 'pinned': e.name in info['pins']} for e in items]
    out['partial_bytes'] = info['partial']
    out['bytes'] = sum(e.bytes for e in items) + info['partial']
    out['free_bytes'] = info['free']
    return out


# ---- removing


def remove(machine, base, names):
    for n in names:
        if not files.valid_name(n):
            raise RuntimeError(f'"{n}" is not an item name: give the name `tutti files list` shows')
    if not names:
        return
    b = shell_path(base)
    paths = ' '.join(f'{b}/{shlex.quote(n)} {b}/{files.META}/{shlex.quote(n)}.json {b}/{files.META}/{shlex.quote(n)}.sha256'
                     for n in names)
    if remote.sh(machine, f'rm -rf {paths}').returncode != 0:
        raise RuntimeError(f'{machine["name"]}: removing {", ".join(names)} from {base} failed: check its permissions')


def remove_partials(machine, base, older_than_minutes=None):
    """Interrupted transfers' partial files: all of them, or only those untouched for a while (abandoned)."""
    age = f' -mmin +{int(older_than_minutes)}' if older_than_minutes else ''
    remote.sh(machine, f'cd {shell_path(base)} && find . -name {shlex.quote("*" + files.PART_SUFFIX)} -type f{age} -delete; '
                       f'find . -path {shlex.quote("*/" + files.PARTIAL + "/*")} -type f{age} -delete; '
                       f'find . -depth -type d -name {files.PARTIAL} -empty -delete')


def clean(cfg, machine, names=(), everything=False):
    """Removes the named items, every item (`everything`) with every partial file, or, with neither, abandoned partial files
    and the least recently used items until the cache fits its budget. Items a running job pinned stay. Returns the names
    removed."""
    s = files.settings(cfg, machine)
    with machine_lock(machine, s['cache']):
        info = inspect(machine, s['cache'])
        entries = [e for e, _ in info['items'].values() if e.name not in info['pins']]
        if everything:
            names = [e.name for e in entries]
            remove_partials(machine, s['cache'])
        elif not names:
            remove_partials(machine, s['cache'], files.PIN_HOURS * 60)
            names, _ = files.plan_eviction(entries, s['budget_gb'] * files.GB, 0)
        missing = [n for n in names if n not in info['items']]
        if missing:
            raise RuntimeError(f'{machine["name"]} has no {", ".join(missing)} in {s["cache"]}: `tutti files list --on '
                               f'{machine["name"]}` shows what it has')
        pinned = [n for n in names if n in info['pins']]
        if pinned:
            raise RuntimeError(f'{", ".join(pinned)} on {machine["name"]} is an input of a running job: clean it after the job ends')
        remove(machine, s['cache'], list(names))
    return list(names)


def pin(machine, base, names, token):
    """Marks the items as inputs of a running job: eviction, clean and a replacing send leave them alone until unpin()."""
    b = f'{shell_path(base)}/{files.META}'
    remote.sh(machine, ' && '.join(f'touch {b}/{shlex.quote(n)}.pin-{shlex.quote(token)}' for n in names))


def unpin(machine, base, names, token):
    b = f'{shell_path(base)}/{files.META}'
    remote.sh(machine, 'rm -f ' + ' '.join(f'{b}/{shlex.quote(n)}.pin-{shlex.quote(token)}' for n in names))


# ---- copying


def rsync_argv(cfg, machine, item, base, s, bwlimit):
    argv = ['rsync', '-a', '--partial', f'--partial-dir={files.PARTIAL}', '--info=progress2', '--no-inc-recursive']
    if item.is_dir:
        argv.append('--delete')
    if s['compress']:
        argv.append('-z')
    if bwlimit:
        argv.append(f'--bwlimit={int(bwlimit)}')
    if machine['transport'] != 'local':
        argv += ['-e', remote.rsh(cfg, machine)]
    return argv + [item.path, rsync_target(machine, base)]


def copy_rsync(cfg, machine, item, base, s, bwlimit, job):
    p = subprocess.Popen(rsync_argv(cfg, machine, item, base, s, bwlimit), stdout=subprocess.PIPE, stderr=job.log)
    while chunk := p.stdout.read1(4096):
        done = files.rsync_progress(chunk.decode('utf8', errors='replace'))
        if done is not None:
            job.progress(min(done, item.bytes))
    if p.wait() != 0:
        raise RuntimeError(f'rsync to {machine["name"]} stopped (exit {p.returncode}); its partial files are kept: send again to '
                           f'resume. The log is {job.log_path}')


def remote_size(machine, path_sh):
    r = remote.sh(machine, f'mkdir -p "$(dirname {path_sh})" && stat -c %s {path_sh} 2>/dev/null || echo 0', capture=True)
    out = r.stdout.strip().splitlines()
    return int(out[-1]) if r.returncode == 0 and out and out[-1].isdigit() else 0


def stream_file(machine, src, size, final_sh, bwlimit, job, sent):
    """Appends the bytes the machine does not have yet to <final>.tutti-part, then moves it into place."""
    part_sh = final_sh + shlex.quote(files.PART_SUFFIX)
    offset = remote_size(machine, part_sh)
    if offset > size:
        offset = 0
        remote.sh(machine, f'rm -f {part_sh}')
    p = subprocess.Popen(remote.argv(machine, f'cat >> {part_sh}'), stdin=subprocess.PIPE)
    start, done = time.monotonic(), 0
    with open(src, 'rb') as f:
        f.seek(offset)
        while block := f.read(CHUNK):
            p.stdin.write(block)
            done += len(block)
            job.progress(sent + offset + done)
            if bwlimit:
                time.sleep(max(0.0, done / (bwlimit * 1024) - (time.monotonic() - start)))
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError(f'the copy of {os.path.basename(src)} to {machine["name"]} stopped (exit {p.returncode}); '
                           f'{offset + done} bytes are kept: send again to resume')
    mtime = int(os.path.getmtime(src))
    if remote.sh(machine, f'mv -f {part_sh} {final_sh} && touch -d @{mtime} {final_sh}').returncode != 0:
        raise RuntimeError(f'{machine["name"]}: could not put {os.path.basename(src)} in place: check the disk and its permissions')


def copy_stream(machine, item, base, bwlimit, job):
    parent = os.path.dirname(item.path)
    if item.dirs:
        remote.sh(machine, 'mkdir -p ' + ' '.join(shell_path(base, d) for d in item.dirs))
    sent = 0
    for rel, size, _ in item.files:
        stream_file(machine, os.path.join(parent, rel), size, shell_path(base, rel), bwlimit, job, sent)
        sent += size
    if item.is_dir:
        drop_extras(machine, base, item)


def drop_extras(machine, base, item):
    """Removes files the folder no longer has, as rsync --delete would; partial files stay for a later resume."""
    r = remote.sh(machine, f'cd {shell_path(base)} && find {shlex.quote(item.name)} -type f', capture=True)
    gone = files.extras(r.stdout.splitlines(), item)
    for i in range(0, len(gone), 200):
        remote.sh(machine, f'cd {shell_path(base)} && rm -f ' + ' '.join(shlex.quote(p) for p in gone[i:i + 200]))


def verify(machine, base, item):
    if not item.files:
        r = remote.sh(machine, f'test -d {shell_path(base, item.name)}')
        return r.returncode == 0, ['the folder is missing']
    r = remote.sh(machine, f'cd {shell_path(base)} && sha256sum -c --quiet --strict -', capture=True, stdin=files.manifest(item))
    return r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[:5]


def forget(machine, base, name):
    """Drops an item's record before its files change: an interrupted update must not pass for the old, intact version."""
    b = f'{shell_path(base)}/{files.META}/{shlex.quote(name)}'
    remote.sh(machine, f'rm -f {b}.json {b}.sha256')


def record(machine, base, item):
    meta = files.meta_json(item, time.time(), os.uname().nodename if hasattr(os, 'uname') else '')
    b = f'{shell_path(base)}/{files.META}/{shlex.quote(item.name)}'
    ok = remote.sh(machine, f'cat > {b}.sha256', stdin=files.manifest(item)).returncode == 0
    if not ok or remote.sh(machine, f'cat > {b}.json', stdin=meta).returncode != 0:
        raise RuntimeError(f'{machine["name"]}: the record of {item.name} could not be written in {base}: check the disk')


def copy_item(cfg, machine, item, base, s, use_rsync, bwlimit, job):
    """Copies and verifies one item; a failed check removes the copy and sends it once more from scratch."""
    for attempt in (1, 2):
        if use_rsync:
            copy_rsync(cfg, machine, item, base, s, bwlimit, job)
        else:
            copy_stream(machine, item, base, bwlimit, job)
        ok, lines = verify(machine, base, item)
        if ok:
            record(machine, base, item)
            return
        job.say(f'{item.name}: the checksum on {machine["name"]} does not match ({"; ".join(lines) or "no output"})'
                + ('; sending it again from scratch' if attempt == 1 else ''))
        remove(machine, base, [item.name])
    raise RuntimeError(f'{item.name} arrived damaged on {machine["name"]} twice: check its disk (`df`, `dmesg`) and the network, '
                       'then send again')


# ---- the send


def prepare(cfg, paths, s):
    """The items to send, after the secret check. Refuses the whole send when any path is or holds a secret."""
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise RuntimeError(f'{", ".join(missing)} not found here: give a file or folder that exists on this computer')
    bad = files.secrets_in(paths, s['refuse'])
    if bad:
        shown = '; '.join(f'{p}: {r}' for p, r in bad[:5]) + (f'; and {len(bad) - 5} more' if len(bad) > 5 else '')
        raise RuntimeError(f'refused, nothing was copied: {shown}. Secrets never leave this computer: move them out of what you '
                           'send, or send the folder without them')
    cache = load_sha_cache(cfg)
    items = [files.item_of(p, cache) for p in paths]
    save_sha_cache(cfg, cache)
    names = [i.name for i in items]
    clash = sorted({n for n in names if names.count(n) > 1})
    if clash:
        raise RuntimeError(f'two items are both called {", ".join(clash)}: send them in separate calls with different --dest')
    bad_names = [n for n in names if not files.valid_name(n)]
    if bad_names:
        raise RuntimeError(f'{", ".join(bad_names)} cannot be an item name: send a named file or folder')
    return items


def sha_cache_path(cfg):
    return os.path.join(config.state_dir(cfg), 'files', 'sha256-cache.json')


def load_sha_cache(cfg):
    try:
        with open(sha_cache_path(cfg), encoding='utf8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_sha_cache(cfg, cache):
    os.makedirs(os.path.dirname(sha_cache_path(cfg)), exist_ok=True)
    with open(sha_cache_path(cfg), 'w', encoding='utf8') as f:
        json.dump(dict(list(cache.items())[-2000:]), f)


def make_room(machine, base, s, info, todo, keep, job):
    """The cache's budget and the disk's free space, before a byte moves. Removes least recently used items when needed.
    A replaced item counts once (its new size); partial files of interrupted transfers count against the budget."""
    incoming = sum(i.bytes for i in todo)
    budget = s['budget_gb'] * files.GB - info['partial']
    replaced = {i.name for i in todo}
    entries = [e for e, _ in info['items'].values() if e.name not in replaced]
    out, fits = files.plan_eviction(entries, budget, incoming, set(keep) | info['pins'])
    if not fits:
        raise RuntimeError(f'{files.human(incoming)} does not fit the {files.human(budget)} cache budget on {machine["name"]}: raise '
                           f'files.budget_gb (or machines.{machine["name"]}.files.budget_gb), or send with --dest <folder>')
    if out:
        job.say(f'{machine["name"]}: removing {", ".join(out)} (least recently used) to stay within {files.human(budget)}')
        remove(machine, base, out)
    freed = sum(e.bytes for e in entries if e.name in out)
    check_free(machine, s, None if info['free'] is None else info['free'] + freed, incoming)


def check_free(machine, s, free, incoming):
    spare = s['keep_free_gb'] * files.GB
    if free is not None and free - incoming < spare:
        raise RuntimeError(f'{machine["name"]} has {files.human(free)} free and this needs {files.human(incoming)} plus '
                           f'{files.human(spare)} spare (files.keep_free_gb): free space there, or `tutti files clean --on '
                           f'{machine["name"]}`')


def send(cfg, machine, paths, dest=None, bwlimit=None, use_rsync=None, pin_token=None):
    """Copies the files and folders to the machine. Returns (folder on the machine, [(name, 'sent' | 'already there', bytes)]).
    With `pin_token` the items are pinned for a job before the lock is let go (unpin() when the job ends)."""
    s = files.settings(cfg, machine)
    base = base_of(cfg, machine, dest)
    bw = s['bwlimit_kbps'] if bwlimit is None else bwlimit
    items = prepare(cfg, paths, s)
    job = Job(cfg, f'files send {", ".join(i.name for i in items)} -> {machine["name"]}:{base}', target=f'machine:{machine["name"]}',
              spec={'kind': 'files', 'machine': machine['name'], 'paths': [i.path for i in items]})
    try:
        with machine_lock(machine, base):
            result = _send(cfg, machine, items, base, s, bw, use_rsync, dest is None, job)
            if pin_token:
                pin(machine, base, [i.name for i in items], pin_token)
    except BaseException as e:
        job.say(f'failed: {e}')
        job.end(1)
        raise
    job.end(0)
    return base, result


def _send(cfg, machine, items, base, s, bw, use_rsync, in_cache, job):
    info = inspect(machine, base)
    have = info['items']
    todo = [i for i in items if i.name not in have or have[i.name][1].get('manifest') != files.identity(i)]
    busy = [i.name for i in todo if i.name in info['pins']]
    if busy:
        raise RuntimeError(f'{", ".join(busy)} on {machine["name"]} is an input of a running job and differs from yours: send it '
                           'after that job ends, or with --dest <folder>')
    if in_cache:
        make_room(machine, base, s, info, todo, [i.name for i in items], job)
    else:
        check_free(machine, s, info['free'], sum(i.bytes for i in todo))
    rsync = (shutil.which('rsync') is not None and info['rsync']) if use_rsync is None else use_rsync
    job.total = sum(i.bytes for i in todo) or 1
    out = []
    for i in items:
        if i not in todo:
            remote.sh(machine, f'touch {shell_path(base)}/{files.META}/{shlex.quote(i.name)}.json')
            job.say(f'{i.name}: already on {machine["name"]} with the same checksums, skipped')
            out.append((i.name, 'already there', i.bytes))
            continue
        job.say(f'{i.name}: {files.human(i.bytes)} to {machine["name"]}:{base} by {"rsync" if rsync else "stream"}')
        forget(machine, base, i.name)
        copy_item(cfg, machine, i, base, s, rsync, bw, job)
        job.base += i.bytes
        job.progress(0)
        out.append((i.name, 'sent', i.bytes))
    job.finish()
    return out
