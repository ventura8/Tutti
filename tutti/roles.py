"""Role eligibility from read-only probe evidence. Scheduling policy lives in routing and devices."""

from . import adbhost, castroles

AGENTS = ('agy', 'claude', 'codex', 'cursor-agent', 'ollama')
BROWSERS = ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser')
TOOLS = (*AGENTS, *BROWSERS, 'git', 'python3', 'java', 'sdkmanager', 'curl', 'sha256sum')
PROBE = (
    'echo "O $(uname -s) $(uname -m)"; '
    + adbhost.FIND + '[ -n "$A" ] && [ -x "$A" ] && echo "K adb"; '
    + 'for tool in ' + ' '.join(TOOLS) + '; do '
    'if command -v "$tool" >/dev/null 2>&1; then echo "K $tool"; fi; done; '
)

MACHINE_REASONS = {
    'host': 'Tutti host prerequisites were not detected',
    'backup_host': 'This machine is outside the failover candidate pool or lacks host prerequisites',
    'worker': 'No supported worker executable was detected',
    'test_runner': 'The Git and Python test tools were not detected',
    'android_build': 'Java and the Android SDK manager were not detected',
    'windows_ui': 'No Windows host or WSL connection was detected',
    'device_host': 'Install Android platform-tools or configure a supported device integration',
    'cast_sender': 'No Cast sender was detected',
    'cast_receiver': 'No Cast receiver was detected',
    'presence_source': 'No idle-time reading is available',
    'energy_meter': 'No measured power source was detected',
    'notification_target': 'No notification integration was detected',
    'dashboard_viewer': 'No supported browser was detected',
    'storage_cache': 'Available disk space is unknown or below the configured minimum',
    'ci_runner': 'The Git and Python build tools were not detected',
}


def matrix(reasons, eligible, unavailable=''):
    """A stable role set, including exclusions and the reason for each."""
    return {role: (role in eligible and not unavailable,
                   unavailable or ('' if role in eligible else need)) for role, need in reasons.items()}


def machine_matrix(row, backup=False, powered=False):
    tools = set(row.get('tools') or ())
    desktop = row.get('transport') != 'android'
    build = {'git', 'python3'} <= tools and desktop
    storage = (row.get('disk_gb') or 0) > 0 and row['disk_gb'] >= row.get('min_free_gb', 0)
    host = build and storage and row.get('transport') in ('local', 'ssh', 'wsl')
    browser = not tools.isdisjoint(BROWSERS)
    cast = row.get('cast_roles') or ([castroles.SENDER] if browser else [])
    eligible = {
        'host': host, 'backup_host': host and backup,
        'worker': bool(tools.intersection(AGENTS)) or row.get('ollama', False),
        'test_runner': build, 'ci_runner': build, 'android_build': desktop and {'java', 'sdkmanager'} <= tools,
        'windows_ui': row.get('transport') == 'wsl' or row.get('os', '').startswith(('MINGW', 'MSYS', 'CYGWIN')),
        'device_host': 'adb' in tools, 'presence_source': row.get('idle_s') is not None, 'energy_meter': powered,
        'cast_sender': castroles.SENDER in cast, 'cast_receiver': castroles.RECEIVER in cast,
        'dashboard_viewer': browser,
        'storage_cache': storage,
    }
    unavailable = '' if row.get('reachable') else 'Machine did not answer the fleet probe'
    return matrix(MACHINE_REASONS, {role for role, ok in eligible.items() if ok}, unavailable)


DEVICE_REASONS = {
    **{role: 'This device has no configured integration for this role' for role in MACHINE_REASONS},
    'test_runner': 'Pair and authorize the device using Wireless debugging',
    'cast_sender': MACHINE_REASONS['cast_sender'],
    'cast_receiver': MACHINE_REASONS['cast_receiver'],
    'presence_source': 'No screen or owner activity reading is available',
}


def device_matrix(readings, cast, kind, held=''):
    eligible = {
        'test_runner': True,
        'cast_sender': castroles.SENDER in cast,
        'cast_receiver': castroles.RECEIVER in cast,
        'presence_source': readings.get('screen') is not None or readings.get('person') is not None,
    }
    unavailable = held or device_unavailable(readings.get('state'))
    reasons = DEVICE_REASONS if kind != 'watch' else {**DEVICE_REASONS, 'worker': 'Watch is reserved for device tests'}
    return matrix(reasons, {role for role, ok in eligible.items() if ok}, unavailable)


def device_unavailable(state):
    if state == 'device':
        return ''
    if state == 'unauthorized':
        return DEVICE_REASONS['test_runner']
    return 'Device did not answer the adb probe. Check its connection and Wireless debugging.'


def can_install_adb(row):
    required = {'curl', 'sha256sum', 'python3'}
    tools = set(row.get('tools') or ())
    return (row.get('os') == 'Linux x86_64' and row.get('transport') in ('local', 'ssh', 'wsl')
            and row.get('status') == 'free' and row.get('busy') == 0 and 'adb' not in tools and required <= tools)


def lines(row):
    for role, (assigned, reason) in row['role_matrix'].items():
        yield f'{row["name"]}  {role:20}  ' + ('has' if assigned else f'off: {reason}')
