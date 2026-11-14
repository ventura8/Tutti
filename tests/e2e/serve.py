"""The browser tests' dashboard (tests/e2e/test_ui.py): the example config in a temp home, sign-in on with a fake Google, and
every probe stubbed, so nothing reaches a real machine. test_ui.py runs it as a child process with HOME and XDG_CONFIG_HOME
in the temp folder and PYTHONPATH at the repository and tests/ (test_auth.py's standard-library test key).

Usage: python3 tests/e2e/serve.py <folder>   (prints one JSON line {"port", "key", "client"} once it serves; stops when stdin
closes)"""
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import yaml
from test_auth import jwks_doc, rsa_key

from tutti import auth, config, dashboard, pacing, routing, state

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLIENT = '1234567890-abcdefgh12345678.apps.googleusercontent.com'
# readings with the float noise sums of shares leave (32.122999999999976 % on the grok-bot card, the owner's report)
NOISY = (('grok-bot', 32.122999999999976), ('claude', 61.400000000000006), ('cursor', 7.3500000000000005),
         ('agy-claude', 0.30000000000000004), ('agy-claude-weekly', 48.9), ('agy-gemini', 12), ('agy-gemini-weekly', 99.99999999999999))


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
    raw['dashboard'].update(probe_seconds=3600, watch_scripts=[], watch_agents=[], task_globs=[])   # this host's processes stay out
    path = os.path.join(home, 'tutti.config.yaml')
    with open(path, 'w', encoding='utf8') as f:
        yaml.safe_dump(raw, f)
    return path


def seed(cfg, now):
    log = state.usage_log_path(cfg)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    for account, used in NOISY:
        for back, part in ((6, 0.5), (3, 0.8), (0, 1.0)):
            pacing.append_log(log, account, used * part if back else used, now - timedelta(hours=back), note='seeded', source='dashboard')


JOBS = (('running', 'ship.sh fix(viewer): a long conventional commit title that has to wrap on a phone instead of running off the edge',
         None), ('done', 'tutti test unit -- python3 -m unittest discover -s tests', 0), ('failed', 'tutti quiet npm run e2e', 1))


def seed_jobs(cfg, now):
    """A running job (this process is its pid), a finished one and a failed one, with long titles and logs."""
    folder = os.path.join(config.expand(cfg['project']['state']), 'quiet')
    os.makedirs(folder, exist_ok=True)
    for i, (_kind, title, rc) in enumerate(JOBS):
        base = os.path.join(folder, f'{int(now.timestamp()) - 600 * (i + 1)}-{os.getpid()}-{i}.log')
        with open(base, 'w', encoding='utf8') as f:
            f.write('\n'.join(f'[gate] step {n} passed with a long log line to wrap: {"x" * 30 * (n % 3)}' for n in range(12)) + '\n')
        with open(base + '.cmd', 'w', encoding='utf8') as f:
            f.write(title + '\n')
        if rc is not None:
            with open(base + '.rc', 'w', encoding='utf8') as f:
                f.write(f'{rc}\n')


def probe(machine):
    if machine.get('host') == 'gpu-box.example':
        return routing.Probe(True, 1, 1.75, 8, False, 61000, gpu_temp=55.5, mem_gb=11.333333333)
    return routing.Probe(True, 2, 0.333333333, 4, False, 48123, mem_gb=6.66666666667)


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
                                    power_fn=lambda m, on: None, ollama_fn=lambda h, p: None)
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
