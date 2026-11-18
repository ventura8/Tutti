"""The self-heal round's `hygiene` check (tutti/hygiene.py holds the decisions): on the main machine, each round, one look at
every machine (its yield flag, load and disks), a hung WSL restarted, and the clean-up when it is due. Every shell call goes
through the round's context (`ctx.sh`, `ctx.win`, `ctx.set_yield`, `ctx.questions`), so tests run it on fake machines."""
import os
import socket
import subprocess
import time

from . import config, durable, hygiene

KINDS = ('local', 'ssh', 'wsl')   # a phone or a household member's machine is not Tutti's to clean


def _sh(machine, script, timeout=120):
    from . import remote
    try:
        return remote.sh(machine, script, capture=True, timeout=timeout, ssh_opts=remote.QUICK)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess([], 255, '', 'no answer')


def _set_yield(machine, on, note=''):
    from . import remote
    return remote.set_yield(machine, on, note)


def _questions(cfg):
    from . import needs
    return needs.load(cfg).get('questions') or []


def _offer(cfg, key, question, why):
    from . import needs
    return needs.ask(cfg, {'id': f'heal:{key}', 'title': question, 'repo': 'tutti'}, question, why, ['delete', 'keep'], 'keep')


def leased(cfg):
    """{machine: number of dispatch leases on it}."""
    from . import dispatch
    out = {}
    for lease in dispatch.read_state(cfg)['leases'].values():
        t = str(lease.get('target') or '')
        if t.startswith('machine:'):
            out[t[8:]] = out.get(t[8:], 0) + 1
    return out


def on_main(cfg):
    from . import failover
    if cfg.get('failover') is None:
        return True
    try:
        return failover.me(cfg) == failover.main_of(cfg)
    except Exception:   # an odd failover block: the round still cleans this machine's own disks next time it is fixed
        return False


def check(ctx):
    from . import procs
    cfg = ctx.cfg
    if not on_main(cfg) or (getattr(ctx, 'sh', None) is None and not procs.daemons_allowed()):
        return []   # in tests and checks only fake machines are looked at: never this computer's Docker
    sh = getattr(ctx, 'sh', None) or _sh
    data, leases, out = hygiene.read(cfg), leased(cfg), []
    for name, m in (cfg.get('machines') or {}).items():
        if m.get('transport', 'ssh') not in KINDS:
            continue
        entry = data.setdefault(name, {})
        if m.get('transport') == 'wsl':   # first: a hung WSL cannot answer the look below (its flag: the last one seen)
            out += wsl_round(ctx, m, entry, held_by(entry, leases.get(name, 0)))
        r = sh(m, hygiene.measure_script(m), 60)
        if r.returncode != 0 or 'HOST ' not in (r.stdout or ''):
            continue   # unreachable: the probe already shows it offline
        meas = hygiene.parse_measure(r.stdout, ctx.now)
        out += flag_round(ctx, m, meas, entry)
        held = held_by(entry, leases.get(name, 0))
        out += disk_round(ctx, m, meas, entry)
        out += clean_round(ctx, m, meas, entry, held)
    hygiene.write(cfg, data)
    return out


def held_by(entry, leases):
    f = entry.get('flag')
    if f:
        return f'its yield flag is up ({f.get("owner") or "owner unknown"}: {f.get("reason") or "no reason given"})'
    return f'{leases} run(s) hold it (leases)' if leases else ''


def flag_round(ctx, m, meas, entry):
    """The flag kept for the card; lowered when it expired or outlived its owner while the machine is idle."""
    f = meas['flag']
    if not f:
        entry['flag'] = None
        return []
    why = hygiene.stale_flag(f, ctx.now, owner_alive(f, meas), hygiene.idle(meas))
    if why and (getattr(ctx, 'set_yield', None) or _set_yield)(m, False) == 0:
        entry['flag'] = None
        return [f'{m["name"]}: {why}: lowered']
    entry['flag'] = {k: f[k] for k in ('owner', 'reason', 'at', 'expires', 'legacy')}
    return []


def owner_alive(f, meas):
    """True or False when the flag's owner process can be looked at (on the flagged machine or here), else None."""
    if f['host'] and f['host'] == meas['host']:
        return meas['pid_alive']
    if f['pid'] and f['host'] == socket.gethostname():
        return durable.pid_alive(int(f['pid']))
    return None


def disk_round(ctx, m, meas, entry):
    """The card's disk line; under WARN_GB the biggest reclaimable items; under STOP_GB one question for the admin."""
    level, free, path, pct = hygiene.disk_level(meas)
    entry['disk'] = {'level': level, 'free_gb': free, 'where': hygiene.label(path), 'pct': pct,
                     'all': {hygiene.label(p): d['free_gb'] for p, d in hygiene.work_disks(meas).items()}}
    if level == 'ok':
        entry['biggest'] = []
        return []
    if ctx.now - float(entry.get('biggest_at') or 0) > 3600:
        entry['biggest'], entry['biggest_at'] = biggest(ctx, m), ctx.now
    if level == 'stop':
        ctx.ask(ctx.cfg, f'diskfull:{m["name"]}', f'Make room on {m["name"]}: {hygiene.label(path)} has {free:.1f} GB free',
                f'Under {hygiene.STOP_GB} GB Tutti sends it no new work. Its own clean-up runs by itself; the biggest items '
                'left are listed on its machine card (Machines).')
    return []


BIGGEST = ('command -v docker >/dev/null 2>&1 && docker system df --format \'{{.Type}}\t{{.Reclaimable}}\' 2>/dev/null | '
           'sed \'s/^/DOCKER\t/\'; du -sk "$HOME"/.cache/* 2>/dev/null | sort -n | tail -n 5 | sed \'s/^/DU\t/\'; true')


def biggest(ctx, m):
    """[text]: the biggest reclaimable items on the machine (Docker, ~/.cache, Remote Desktop traces)."""
    r = (getattr(ctx, 'sh', None) or _sh)(m, BIGGEST, 120)
    items = []
    for ln in (r.stdout or '').splitlines():
        w = ln.split('\t')
        if w[0] == 'DOCKER' and len(w) > 2 and not w[2].startswith('0B'):
            items.append(f'Docker {w[1].lower()}: {w[2]} reclaimable')
        elif w[0] == 'DU' and len(w) > 2 and hygiene._num(w[1]) > 1048576:
            items.append(f'{w[2]}: {hygiene._num(w[1]) / 1048576:.1f} GB')
    if m.get('transport') == 'wsl':
        gb = trace_gb(ctx, m)
        if gb >= 1:
            items.append(f'Remote Desktop traces on Windows: {gb:.1f} GB')
    return items[:8]


def _win(ctx, m, command, timeout):
    return (getattr(ctx, 'win', None) or hygiene.win_ssh)(m, command, timeout)


def trace_gb(ctx, m):
    try:
        r = _win(ctx, m, hygiene.TRACE_SIZE, 120)
    except (OSError, subprocess.SubprocessError):
        return 0.0
    return hygiene._num((r.stdout or '0').strip().splitlines()[-1] if (r.stdout or '').strip() else '0') / 1e9


def wsl_round(ctx, m, entry, held):
    state = hygiene.wsl_state(lambda c, t: _win(ctx, m, c, t))
    if state != 'hung':
        if state == 'ok':
            entry['wsl'] = None
        return []
    if held:
        entry['wsl'] = f'WSL hung: not restarted while {held}'
        return []
    try:
        _win(ctx, m, 'wsl --shutdown', 120)
        _win(ctx, m, 'wsl -e true', 120)
    except (OSError, subprocess.SubprocessError) as e:
        entry['wsl'] = f'WSL hung: the restart did not finish ({type(e).__name__}); tried again next round'
        return []
    entry['wsl'] = f'WSL hung: restarted at {time.strftime("%H:%M", time.localtime(ctx.now))}'
    return [f'{m["name"]}: WSL hung (Windows answered, WSL did not): shut down and started again']


LOW_EVERY = 3600   # under CLEAN_PCT % free the clean-up runs again at most hourly, not every round


def due(entry, meas, now):
    low = any(d['pct'] < hygiene.CLEAN_PCT for d in hygiene.work_disks(meas).values())
    since = now - float(entry.get('cleaned_at') or 0)
    return since >= (LOW_EVERY if low else hygiene.CLEAN_EVERY)


def clean_round(ctx, m, meas, entry, held):
    if not due(entry, meas, ctx.now):
        return []
    if held:
        entry['note'] = f'clean-up waits: {held}'
        return []
    entry['note'] = None
    sh = getattr(ctx, 'sh', None) or _sh
    listing = sh(m, hygiene.IMAGES_SCRIPT, 120)
    refs = hygiene.images_to_delete(listing.stdout or '', hygiene.keep_images(ctx.cfg), ctx.now)
    r = sh(m, hygiene.linux_clean_script([ref for ref, _ in refs]), 1800)
    gb, done = hygiene.freed_kb(r.stdout) / 1048576, []
    gone = sum(1 for ln in (r.stdout or '').splitlines() if ln.startswith('RMI '))
    if gone:
        done.append(f'{gone} old Docker image(s)')
    if m.get('transport', 'ssh') == 'local':
        gb += local_files(ctx, done)
    if m.get('transport') == 'wsl':
        gb += windows_round(ctx, m, entry, done)
        if gb >= hygiene.COMPACT_GB and hygiene.idle(meas):
            done.append(compact(ctx, m))
    entry['cleaned_at'] = ctx.now
    entry['reclaimed'] = [*(entry.get('reclaimed') or []), [ctx.now, round(gb, 2)]][-60:]
    if gb < 0.1 and not done:
        return []
    return [f'{m["name"]}: clean-up reclaimed {gb:.1f} GB' + (f' ({", ".join(x for x in done if x)})' if done else '')]


def local_files(ctx, done):
    """Tutti's old run and quiet logs and its clean run worktrees, on this machine. Returns GB freed (logs only, measured)."""
    state = config.state_dir(ctx.cfg)
    files, size = hygiene.old_files([os.path.join(state, 'runs'), os.path.join(state, 'quiet')], ctx.now)
    for p in files:
        try:
            os.remove(p)
        except OSError:
            continue
    if files:
        done.append(f'{len(files)} old log(s)')
    remove = getattr(ctx, 'remove_worktree', None) or hygiene.remove_worktree
    busy = (getattr(ctx, 'busy_dirs', None) or hygiene.busy_dirs)()
    legacy = config.expand((ctx.cfg.get('project') or {}).get('worktrees') or '')
    found = hygiene.old_worktrees(os.path.join(state, 'work'), ctx.now, busy, nested=True)
    if legacy:
        found += hygiene.old_worktrees(legacy, ctx.now, busy)
    trees = [p for p in found if remove(p)]
    if trees:
        done.append(f'{len(trees)} old run worktree(s)')
    return size / 1e9


def windows_round(ctx, m, entry, done):
    """A WSL machine's Windows side: APK outputs and caches; Remote Desktop traces on the owner's yes. Returns GB freed."""
    roots = [p['windows'][m['name']] for p in (ctx.cfg.get('projects') or {}).values()
             if isinstance(p, dict) and isinstance(p.get('windows'), dict) and p['windows'].get(m['name'])]
    gb = 0.0
    try:
        r = _win(ctx, m, hygiene.windows_clean(roots), 1800)
        gb = max(0.0, hygiene._num(next((ln.split()[1] for ln in (r.stdout or '').splitlines() if ln.startswith('FREED ')), '0')) / 1e9)
    except (OSError, subprocess.SubprocessError):
        done.append('Windows side did not answer')
    return gb + traces(ctx, m, entry, done)


def traces(ctx, m, entry, done):
    """Remote Desktop traces: offered once they reach TRACE_ASK_GB; deleted after the owner answers `delete`."""
    size = trace_gb(ctx, m)
    key = f'rdtrace:{m["name"]}'
    asked = [q for q in (getattr(ctx, 'questions', None) or _questions)(ctx.cfg) if q.get('task') == f'heal:{key}']
    yes = [q for q in asked if q.get('status') == 'answered' and q.get('answer') == 'delete'
           and float(q.get('answered') or 0) > float(entry.get('rd_done') or 0)]
    if yes and size > 0:
        try:
            _win(ctx, m, hygiene.TRACE_DELETE, 600)
        except (OSError, subprocess.SubprocessError):
            return 0.0
        entry['rd_done'] = ctx.now
        done.append(f'Remote Desktop traces ({size:.1f} GB, on your yes)')
        return size
    if size >= hygiene.TRACE_ASK_GB and not any(q.get('status') == 'open' for q in asked):
        why = ('The Remote Desktop client keeps diagnostic traces in %TEMP%\\DiagOutputDir\\RdClientAutoTrace and never deletes '
               'them; nothing needs them. Answer delete and Tutti removes them next round.')
        (getattr(ctx, 'offer', None) or _offer)(ctx.cfg, key, f'Delete {size:.0f} GB of Remote Desktop traces on {m["name"]}?', why)
    return 0.0


def compact(ctx, m):
    try:
        r = _win(ctx, m, hygiene.COMPACT, 1800)
    except (OSError, subprocess.SubprocessError):
        return 'the WSL disk was not compacted (Windows did not answer)'
    if 'COMPACTED' in (r.stdout or ''):
        return 'the WSL disk compacted'
    return 'the WSL disk set to give space back by itself (sparse)' if 'SPARSE' in (r.stdout or '') else ''
