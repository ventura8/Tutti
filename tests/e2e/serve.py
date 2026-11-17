"""The browser tests' dashboard (tests/e2e/test_ui.py): the example config in a temp home, sign-in on with a fake Google, and
every probe stubbed, so nothing reaches a real machine. test_ui.py runs it as a child process with HOME and XDG_CONFIG_HOME
in the temp folder and PYTHONPATH at the repository and tests/ (test_auth.py's standard-library test key).

With TUTTI_E2E_FLEET=owner the data has the shapes of the owner's own dashboard (2026-11-16): seven machines, four
accounts in panic at 97-100 %, eight jobs of which three stalled, long routing reasons and a config edit that does not load.

Usage: python3 tests/e2e/serve.py <folder>   (prints one JSON line {"port", "key", "client"} once it serves; stops when stdin
closes)"""
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import yaml
from test_auth import jwks_doc, rsa_key

from tutti import auth, config, dashboard, energy, pacing, routing, state

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLIENT = '1234567890-abcdefgh12345678.apps.googleusercontent.com'
# readings with the float noise sums of shares leave (32.122999999999976 % on the grok-bot card, the owner's report)
NOISY = (('grok-bot', 32.122999999999976), ('claude', 61.400000000000006), ('cursor', 7.3500000000000005),
         ('agy-claude', 0.30000000000000004), ('agy-claude-weekly', 48.9), ('agy-gemini', 12), ('agy-gemini-weekly', 99.99999999999999))
OWNER = os.environ.get('TUTTI_E2E_FLEET') == 'owner'
PANIC = (('grok-bot', 32.1), ('claude', 99.6), ('cursor', 41.2), ('agy-claude', 97.3), ('agy-claude-weekly', 100.0),
         ('agy-gemini', 98.4), ('agy-gemini-weekly', 64.0))
MORE_MACHINES = ('nuc', 'laptop', 'pc', 'mac-mini')


class Readers:
    """No reader command runs: the readings are the seeded usage log."""

    cfg = None

    def read_all(self, _now, _force=False):
        return {}

    def scan_jobs(self, _now):
        return []


def write_config(home):
    with open(os.path.join(ROOT, 'tutti.config.example.yaml'), encoding='utf8') as f:
        raw = yaml.safe_load(f)
    raw['auth'] = {'client_id': CLIENT}
    raw['fleet'] = {'enabled': False}   # no tunnel is ever started from a test
    # a dedicated desktop session on gpu-box's computer with its own model worker: it must fold into gpu-box's one card
    raw['machines']['gpu-box-desktop'] = {**raw['machines']['gpu-box'], 'dedicated': True}
    raw['workers']['big-desktop'] = {**raw['workers']['big'], 'machine': 'gpu-box-desktop', 'host': '127.0.0.1:11453'}
    raw['dashboard'].update(probe_seconds=3600, watch_scripts=[], watch_agents=[], task_globs=[])   # this host's processes stay out
    if OWNER:
        for name in MORE_MACHINES:
            raw['machines'][name] = {'host': f'{name}.example', 'slots': 2}
    path = os.path.join(home, 'tutti.config.yaml')
    with open(path, 'w', encoding='utf8') as f:
        yaml.safe_dump(raw, f)
    return path


def seed(cfg, now):
    log = state.usage_log_path(cfg)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    for account, used in PANIC if OWNER else NOISY:
        for back, part in ((6, 0.5), (3, 0.8), (0, 1.0)):
            pacing.append_log(log, account, used * part if back else used, now - timedelta(hours=back), note='seeded', source='dashboard')


JOBS = (('running', 'ship.sh fix(viewer): a long conventional commit title that has to wrap on a phone instead of running off the edge',
         None), ('done', 'tutti test unit -- python3 -m unittest discover -s tests', 0), ('failed', 'tutti quiet npm run e2e', 1))


OWNER_JOBS = JOBS + (('stalled', 'tutti test e2e --device pixel -- ./gradlew connectedAndroidTest', None),
                     ('stalled', 'ship.sh feat(cast): the receiver reconnects after the TV wakes from standby', None),
                     ('stalled', 'tutti quiet dotnet test RoRadioTestProject', None),
                     ('done', 'tutti run auto "triage the newest Sentry issues"', 0), ('done', 'tutti files send models/ nuc', 0))


def seed_jobs(cfg, now):
    """A running job (this process is its pid), a finished one and a failed one, with long titles and logs; the owner's fleet
    adds three stalled jobs (their logs untouched for an hour) and two more finished ones."""
    folder = os.path.join(config.expand(cfg['project']['state']), 'quiet')
    os.makedirs(folder, exist_ok=True)
    for i, (kind, title, rc) in enumerate(OWNER_JOBS if OWNER else JOBS):
        base = os.path.join(folder, f'{int(now.timestamp()) - 600 * (i + 1)}-{os.getpid()}-{i}.log')
        with open(base, 'w', encoding='utf8') as f:
            f.write('\n'.join(f'[gate] step {n} passed with a long log line to wrap: {"x" * 30 * (n % 3)}' for n in range(12)) + '\n')
        with open(base + '.cmd', 'w', encoding='utf8') as f:
            f.write(title + '\n')
        if rc is not None:
            with open(base + '.rc', 'w', encoding='utf8') as f:
                f.write(f'{rc}\n')
        if kind == 'stalled':
            old = now.timestamp() - 3600
            for path in (base, base + '.cmd'):
                os.utime(path, (old, old))


def probe(machine):
    if machine.get('host') == 'gpu-box.example':
        return routing.Probe(True, 1, 1.75, 8, False, 61000, gpu_temp=55.5, mem_gb=11.333333333)
    return routing.Probe(True, 2, 0.333333333, 4, False, 48123, mem_gb=6.66666666667)


POWER = {'gpu-box.example': '== nvidia\nNVIDIA GeForce RTX 5090, 75.91, 12, 600.00\n== end',
         'local': '== intel\nFailed to initialize PMU! (Permission denied)\n== denied\nrapl\n'
                  '== lspci\n00:02.0 VGA compatible controller: Intel Corporation Arrow Lake-P [Arc Pro 130T/140T] (rev 03)\n== end'}


def power(machine, _on):
    text = POWER.get(machine.get('host'))
    return energy.parse_power(text) if text else None


def ollama(host, path):
    if path == '/api/tags':
        return {'models': [{'name': 'qwen3.8:27b-q8_0'}, {'name': 'qwen3.5:9b'}]}
    return {'models': [{'name': 'qwen3.5:9b', 'size_vram': 7257834123}]} if host.endswith('11436') else {'models': []}


def available(worker):
    if worker.get('kind') == 'ollama':
        return False, f'{worker.get("host")} does not answer (connection refused): start the model server or its tunnel'
    return True, 'ok'


def main(folder):
    path = write_config(os.path.abspath(folder))
    cfg = config.load(path)
    seed(cfg, datetime.now(timezone.utc))
    seed_jobs(cfg, datetime.now(timezone.utc))
    collector = dashboard.Collector(cfg, probe_fn=probe, available_fn=available, stored_fn=lambda m: None, reader=Readers(),
                                    activity_fn=lambda *a, **k: {}, prs_fn=lambda c: {}, readings_fn=lambda m, d: {'state': 'unreachable'},
                                    power_fn=power, ollama_fn=ollama)
    collector.refresh()
    if OWNER:   # an edit that does not load: the banner must say what is wrong and how to fix it
        with open(path, 'a', encoding='utf8') as f:
            f.write('machines: [this is not a mapping\n')
        collector.config_mtime = None
        collector.refresh()
    httpd = dashboard.server(cfg, 0, collector)
    key = rsa_key()
    httpd.auth.jwks = auth.Jwks(fetch=lambda: (jwks_doc(key), 3600))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    sys.stdout.write(json.dumps({'port': httpd.server_address[1], 'key': [str(k) for k in key], 'client': CLIENT}) + '\n')
    sys.stdout.flush()
    sys.stdin.read()
    httpd.shutdown()


if __name__ == '__main__':
    main(sys.argv[1])
