"""The dashboard's Pair a device and Move devices buttons (POST /api/devices/pair and /api/devices/move): the same code as
`tutti devices pair` and `tutti devices move`. One pairing at a time runs in a background thread; the page asks its
state every two seconds. A move's plan (a dry run) comes back at once; the move itself runs `tutti devices move` in the
background, its log in <state>/devices-move.log."""
import json
import os
import subprocess
import sys
import threading
import time

from . import adbhost, adbpair, config, devicescmd, devmove, qr

PAIR = {'current': None}


class Pairing:
    """One pairing: waits for the device in a thread, then saves it in the config."""

    def __init__(self, cfg, machine, flow, kind, host):
        self.cfg, self.machine, self.flow, self.kind, self.host = cfg, machine, flow, kind, host
        self.state, self.message, self.device = 'waiting', '', None
        self.code, self.got_code, self.cancelled = None, threading.Event(), False
        self.name, self.password = adbpair.session() if flow == 'qr' else (None, None)

    def view(self):
        out = {'state': self.state, 'message': self.message, 'device': self.device, 'flow': self.flow, 'machine': self.machine,
               'steps': adbpair.steps(self.kind, self.flow)}
        if self.flow == 'qr' and self.state == 'waiting':
            payload = adbpair.qr_payload(self.name, self.password)
            out.update(payload=payload, matrix=qr.matrix(payload))
        return out

    def sleep(self, seconds):
        if self.cancelled:
            raise adbpair.PairError('cancelled')
        time.sleep(seconds)

    def run(self, wait=180):
        try:
            before = adbpair.baseline(self.host)   # before the user opens the device's pairing screen
            if self.flow == 'code':
                self.message = 'type the 6-digit code the device shows'
                if not self.got_code.wait(wait):
                    raise adbpair.PairError('no code was typed: open the pairing screen on the device and start again')
                if self.cancelled:
                    raise adbpair.PairError('cancelled')
                serial, live = adbpair.pair(self.host, code=self.code, ignore=before, wait=wait, sleep=self.sleep, say=self.note)
            else:
                serial, live = adbpair.pair(self.host, name=self.name, password=self.password, wait=wait, sleep=self.sleep,
                                            say=self.note)
            if self.cancelled:
                raise adbpair.PairError('cancelled')
            info = adbpair.describe(self.host, live)
            name = devmove.save_device(self.cfg['_path'], self.cfg.get('devices'), self.machine, serial, info)
            self.device = {'name': name, 'serial': serial, **info}
            self.state, self.message = 'paired', f'{name} ({info["model"]}, {info["kind"]}) is paired on {self.machine} and saved'
        except (adbpair.PairError, OSError, ValueError) as e:
            self.state, self.message = 'failed', str(e)

    def note(self, msg):
        self.message = msg


def _body(raw):
    try:
        data = json.loads(raw or b'null')
    except ValueError:
        data = None
    return data if isinstance(data, dict) else {}


def _start(cfg, data, host_fn, start):
    if PAIR['current'] and PAIR['current'].state == 'waiting':
        return 409, {'error': 'a pairing is already waiting: finish it on the device, or cancel it first'}
    machine = data.get('machine') or devicescmd.default_machine(cfg)
    if machine not in cfg['machines']:
        return 400, {'error': f'no machine "{machine}": the machines are {", ".join(cfg["machines"])}'}
    try:
        flow = adbpair.flow_for(data.get('kind'), data.get('flow'))
    except adbpair.PairError as e:
        return 400, {'error': str(e)}
    if flow == 'usb':
        return 400, {'error': 'the USB fallback runs in a terminal: tutti devices pair --flow usb'}
    host = (host_fn or (lambda n: adbhost.Host(n, cfg['machines'][n])))(machine)
    if not host.path():
        return 400, {'error': f'adb is not on {machine}: run `tutti devices pair --install-adb` once (a user-space copy)'}
    if host.version() < adbpair.MIN_ADB:
        return 400, {'error': f'adb on {machine} is too old for wireless pairing (platform-tools {adbpair.MIN_ADB}+ needed): '
                              'run `tutti devices pair --install-adb` once'}
    p = PAIR['current'] = Pairing(cfg, machine, flow, data.get('kind'), host)
    if start:
        threading.Thread(target=p.run, daemon=True).start()
    return 202, p.view()


def _code(cur, data):
    code = str(data.get('code') or '').strip()
    if not (code.isdigit() and len(code) == 6):
        return 400, {'error': 'type the 6 digits the device shows'}
    cur.code = code
    cur.got_code.set()
    return 200, cur.view()


def _cancel(cur, _data):
    cur.cancelled = True
    cur.state, cur.message = 'failed', 'cancelled'
    cur.got_code.set()
    return 200, cur.view()


def pair_post(collector, raw, host_fn=None, start=True):
    """{"action": "start", "machine"?, "kind"?: "tv", "flow"?} | {"action": "code", "code"} | {"action": "status"|"cancel"}."""
    data = _body(raw)
    if data.get('action') == 'start':
        return _start(collector.cfg, data, host_fn, start)
    cur = PAIR['current']
    if not cur:
        return 404, {'error': 'no pairing is running: press Pair a device'}
    act = {'code': _code, 'cancel': _cancel}.get(data.get('action'))
    return act(cur, data) if act else (200, cur.view())


MOVE = {'proc': None, 'log': ''}
HOSTS = None   # a test's fake adb hosts: (name, adb=None) -> adbhost.Host-like (tests/e2e/serve.py)
POPEN = None   # a test's fake process starter (tests/e2e/serve.py)


def _hosts(cfg, hosts_fn):
    return hosts_fn or HOSTS or (lambda n, adb=None: adbhost.Host(n, cfg['machines'][n], adb=adb))


def move_plan(cfg, to, names, key, hosts_fn=None):
    """The dry run: the move's plan and problems (devices not on `to` yet) and the consolidation's (every device on `to`:
    other machines' stray connections, runners switched to `to`'s adb). `todo` is false when there is nothing to do."""
    found = devmove.pick(cfg, to, names, none_ok=True)
    mk = _hosts(cfg, hosts_fn)
    out = {'plan': [], 'problems': [], 'key': '', 'moves': [d['name'] for d in found]}
    if found:
        old, new = mk(found[0]['machine'], found[0].get('adb')), mk(to)
        pre = devmove.preflight(cfg, to, found, old, new, 'keep' if key == 'keep' else None)
        out.update(plan=devmove.plan_lines(pre, found), problems=pre['problems'], key=pre['key'])
    on_to = [d for d in devmove.named(cfg, to, names) if d['machine'] == to]
    if on_to:
        target = mk(to, next((d.get('adb') for d in on_to if d.get('adb')), None))
        cons = devmove.scan(cfg, to, on_to, mk, target)
        out['plan'] += devmove.consolidate_lines(cons)
        out['strays'] = {m: [s for s, _ in hits] for m, hits in cons['strays'].items()}
        out['skipped'], out['runners'] = cons['skipped'], cons['runners']
        out['todo'] = bool(found) or devmove.pending(cons)
    else:
        out.update(strays={}, skipped={}, runners=[], todo=bool(found))
    if not out['todo']:
        out['plan'].append(f'nothing to do: every device named is on {to} and connected there only')
    return out


def move_status():
    """{"running", "exit", "lines"}: the move started from the page, and its log's last lines."""
    proc, lines = MOVE['proc'], []
    if MOVE['log'] and os.path.exists(MOVE['log']):
        with open(MOVE['log'], encoding='utf8', errors='replace') as f:
            lines = [ln.rstrip() for ln in f.readlines()[-40:]]
    rc = proc.poll() if proc else None
    return 200, {'running': bool(proc) and rc is None, 'exit': rc, 'lines': lines}


def move_post(collector, raw, hosts_fn=None, popen=None):
    """{"to", "devices"?: [...], "dry_run"?: true, "key"?: "keep"}: the plan, or the move started in the background;
    {"action": "status"}: its progress. Admin only (dashboard.ADMIN_PATHS)."""
    data, cfg = _body(raw), collector.cfg
    if data.get('action') == 'status':
        return move_status()
    names = [n for n in data.get('devices') or [] if isinstance(n, str)]
    to = str(data.get('to') or '')
    try:
        plan = move_plan(cfg, to, names, data.get('key'), hosts_fn)
    except devmove.MoveError as e:
        return 400, {'error': str(e)}
    if data.get('dry_run', True):
        return 200, plan
    if plan['problems'] or not plan['todo']:
        return 409, {'error': 'nothing to do' if not plan['todo'] else 'cannot move yet: ' + '; '.join(plan['problems'])}
    if MOVE['proc'] and MOVE['proc'].poll() is None:
        return 409, {'error': 'a move is already running: wait for it to end'}
    from . import remote
    me = [remote.BIN] if os.path.isfile(remote.BIN) else ['-c', 'import sys; from tutti.cli import entry; sys.exit(entry())']
    argv = [sys.executable, *me, '--config', cfg['_path'], 'devices', 'move', '--to', to, *names]
    argv += ['--keep-key'] if data.get('key') == 'keep' else []
    log = os.path.join(config.state_dir(cfg), 'devices-move.log')
    start = popen or POPEN or subprocess.Popen
    try:
        with open(log, 'w', encoding='utf8') as out:
            MOVE['proc'] = start(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        MOVE['log'] = log
    except OSError as e:
        return 500, {'error': f'the move could not start ({e}): run `tutti devices move --to {to}` in a terminal'}
    return 202, {'ok': True, 'log': log}
