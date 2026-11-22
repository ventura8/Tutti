"""The role matrix (t234, docs/MACHINES.md, *Role matrix*): for every machine, every role Tutti knows, whether it has it,
could have it (detected but turned off), or cannot, with the reason and the one command or setting that fixes it.

Pure: the config, the daily probe's facts (<state>/roles-machines.json, kept by tutti/roles.py) and the effective roles in;
rows out. A machine never probed says so and how to probe it."""
from . import roles

MACHINE_ROLES = ('main', 'backup-main', 'chrome-browser', 'build-machine', 'gpu-ai', 'docker-host', 'linux-test-runner',
                 'windows-test-runner', 'macos-test-runner', 'android-test-runner', 'emulator-host', 'ios-webkit-runner',
                 'ui-desktop', 'cast-sender', 'vpn-node')
API = '/api/role-matrix'


def _why_not(role, f, name):
    """(why it cannot, the fix) from the probe's facts `f`; the fix is '' when nothing Tutti could tell would change it."""
    cores, mem = roles._num(f.get('cores')), roles._num(f.get('mem_gb'))
    os_name = 'WSL' if f.get('wsl') else {'Linux': 'Linux', 'Darwin': 'macOS'}.get(f.get('os'), f.get('os') or 'another OS')
    table = {
        'main': ('Tutti runs on another machine', f'`tutti move --to {name}` makes it the main machine (docs/MOVE.md)'),
        'backup-main': ('failover.backup names another machine', f'set failover.backup to {name} on Settings'),
        'chrome-browser': ('browser.machine names another machine', f'set browser.machine to {name} on Settings'),
        'build-machine': (f'{cores:.0f} cores and {mem:.0f} GB; a build machine needs 8 cores and 16 GB', ''),
        'gpu-ai': ('no GPU with 6 GB and no Ollama', 'install Ollama (https://ollama.com/download); a GPU with 6 GB or more runs '
                                                    'the bigger models'),
        'docker-host': ('docker is not installed', 'install Docker Engine (https://docs.docker.com/engine/install/)'),
        'linux-test-runner': (f'it runs {os_name}', ''),
        'windows-test-runner': (f'it runs {os_name}, with no Windows next to it', ''),
        'macos-test-runner': (f'it runs {os_name}', ''),
        'android-test-runner': ('adb is not installed', f'`tutti devices --install-adb` on {name}, then plug a device in'),
        'emulator-host': ('no Android emulator with an AVD', 'install the Android SDK emulator and create an AVD (Android '
                                                            'Studio, Device Manager)'),
        'ios-webkit-runner': ('no Xcode and no Playwright WebKit', 'npx playwright install webkit'),
        'ui-desktop': ('no desktop session is running', 'sign in to a desktop on it (or keep one running)'),
        'cast-sender': ('no Chrome or Chromium', 'install Chrome or Chromium, or set desktop_sender: true for its own sender'),
        'vpn-node': ('not on the private network', '`tutti vpn setup` (docs/VPN.md)'),
    }
    return table.get(role, ('not detected', ''))


def machine_rows(cfg, name, m, rec):
    """[{role, state: has | off | cannot, why, fix}] for one machine; `rec` is its probe record ({at, detected, facts})."""
    detected = {**((rec or {}).get('detected') or {}), **roles.from_config(cfg, name, m)}
    have = {r['role']: r['why'] for r in roles.effective(m, detected)}
    facts = (rec or {}).get('facts')
    out = []
    for role in MACHINE_ROLES:
        if role in have:
            out.append({'role': role, 'state': 'has', 'why': have[role], 'fix': ''})
        elif role in detected:
            out.append({'role': role, 'state': 'off', 'why': 'detected, but turned off for it (roles_off or roles:)',
                        'fix': f'tutti roles set {name} {role}'})
        elif facts is None and role not in ('main', 'backup-main', 'chrome-browser') and m.get('transport') != 'android':
            out.append({'role': role, 'state': 'cannot', 'why': 'not probed yet (or it did not answer)',
                        'fix': 'tutti machines --roles --refresh'})
        else:
            why, fix = _why_not(role, facts or {}, name)
            out.append({'role': role, 'state': 'cannot', 'why': why, 'fix': fix})
    return out


def matrix(cfg, seen):
    """[{name, has, off, cannot, rows}] for every machine; `seen` is <state>/roles-machines.json."""
    out = []
    for name, m in (cfg.get('machines') or {}).items():
        rows = machine_rows(cfg, name, m or {}, (seen or {}).get(name))
        count = {s: sum(1 for r in rows if r['state'] == s) for s in ('has', 'off', 'cannot')}
        out.append({'name': name, **count, 'rows': rows})
    return out


def get(cfg):
    """(status, answer) for GET /api/role-matrix: from the last daily probe, never probing on the request path."""
    import os

    from . import config
    seen = roles._read(os.path.join(config.state_dir(cfg), roles.FILE))
    return 200, {'machines': matrix(cfg, seen), 'roles': {r: roles.ROLES[r] for r in MACHINE_ROLES}}
