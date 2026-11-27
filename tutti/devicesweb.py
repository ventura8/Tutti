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

from . import adbhost, adbpair, config, devicescmd, devmove, qr, roles

PAIR = {'current': None}
ADB_INSTALL = threading.Lock()


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


def _install_adb(collector, name, host_fn):
    if not isinstance(name, str) or name not in collector.cfg['machines']:
        return 400, {'error': 'Choose a configured machine before installing Android platform-tools'}
    if not ADB_INSTALL.acquire(blocking=False):
        return 409, {'error': 'An Android platform-tools install is running. Wait for it to finish and try again.'}
    try:
        return _install_on_idle_machine(collector, name, host_fn)
    finally:
        ADB_INSTALL.release()


def _install_on_idle_machine(collector, name, host_fn):
    row = next((r for r in collector.snapshot()['machines']['items'] if r['name'] == name), {})
    if not roles.can_install_adb(row):
        return 409, {'error': 'The machine must be free, Linux x86_64, and missing adb. Check its role matrix before retrying.'}
    host = host_fn(name) if host_fn else adbhost.Host(name, collector.cfg['machines'][name])
    if host.path():
        return 200, {'ok': True}
    code, _ = host.install_tools()
    if code != 0:
        return 502, {'error': 'Android platform-tools installation failed. Check connectivity and run tutti devices pair --install-adb.'}
    return 200, {'ok': True}


def pair_post(collector, raw, host_fn=None, start=True):
    """Start, finish or cancel wireless pairing, or install pinned adb on an eligible machine."""
    data = _body(raw)
    if data.get('action') == 'start':
        return _start(collector.cfg, data, host_fn, start)
    if data.get('action') == 'install-adb':
        return _install_adb(collector, data.get('machine'), host_fn)
    cur = PAIR['current']
    if not cur:
        return 404, {'error': 'no pairing is running: press Pair a device'}
    act = {'code': _code, 'cancel': _cancel}.get(data.get('action'))
    return act(cur, data) if act else (200, cur.view())


def move_post(collector, raw, hosts_fn=None, popen=subprocess.Popen):
    """{"to", "devices"?: [...], "dry_run"?: true, "key"?: "replace"}: the plan, or the move started in the background."""
    data, cfg = _body(raw), collector.cfg
    names = [n for n in data.get('devices') or [] if isinstance(n, str)]
    try:
        found = devmove.pick(cfg, str(data.get('to') or ''), names)
    except devmove.MoveError as e:
        return 400, {'error': str(e)}
    if data.get('dry_run', True):
        mk = hosts_fn or (lambda n, adb=None: adbhost.Host(n, cfg['machines'][n], adb=adb))
        old, new = mk(found[0]['machine'], found[0].get('adb')), mk(data['to'])
        pre = devmove.preflight(cfg, data['to'], found, old, new, 'keep' if data.get('key') == 'keep' else None)
        return 200, {'plan': devmove.plan_lines(pre, found), 'problems': pre['problems'], 'key': pre['key']}
    from . import remote
    me = [remote.BIN] if os.path.isfile(remote.BIN) else ['-c', 'import sys; from tutti.cli import entry; sys.exit(entry())']
    argv = [sys.executable, *me, '--config', cfg['_path'], 'devices', 'move', '--to', data['to'], *[d['name'] for d in found]]
    argv += ['--keep-key'] if data.get('key') == 'keep' else []
    log = os.path.join(config.state_dir(cfg), 'devices-move.log')
    try:
        with open(log, 'ab') as out:
            popen(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return 500, {'error': f'the move could not start ({e}): run `tutti devices move --to {data["to"]}` in a terminal'}
    return 202, {'ok': True, 'log': log}
