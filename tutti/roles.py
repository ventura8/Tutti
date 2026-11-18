"""Automatic role assignment for machines and devices based on capabilities."""

ROLES = (
    'host', 'worker', 'test_runner', 'device_host', 'cast_receiver',
    'presence_source', 'energy_meter', 'notification_target',
    'dashboard_viewer', 'storage_cache', 'ci_runner'
)

def check_roles(info, kind='machine'):
    """Returns {role: (bool, reason)} for all roles."""
    roles = {}
    for role in ROLES:
        roles[role] = _check_role(role, info, kind)
    return roles

def _check_worker(info, kind):
    if kind == 'watch':
        return False, 'watch too weak'
    if info.get('gpu') or info.get('npu') or 'ollama' in info.get('loaded', []):
        return True, ''
    return False, 'no GPU or NPU detected'

def _check_host(info, kind):
    return (True, '') if info.get('kind') == 'machine' else (False, 'only machines can be hosts')

def _check_device_host(info, kind):
    if 'adb' in info.get('loaded', []) or kind in ('phone', 'tablet', 'watch'):
        return True, ''
    return False, 'adb not detected'

def _check_cast_receiver(info, kind):
    if info.get('kind') == 'tv':
        return True, ''
    return False, 'not a TV'

def _check_test_runner(info, kind):
    has_adb = 'adb' in info.get('loaded', [])
    has_browser = 'browser' in info.get('loaded', [])
    return (True, '') if (has_adb or has_browser) else (False, 'adb or browser required')

def _check_presence_source(info, kind):
    # Presence detection requires sensors/probes.
    # Assume machine/device with sensors has 'sensors' in loaded.
    return 'sensors' in info.get('loaded', []), 'no sensors detected'

def _check_energy_meter(info, kind):
    # Energy meter integration
    return 'energy_meter' in info.get('loaded', []), 'no energy meter detected'

def _check_notification_target(info, kind):
    # Notification support
    return True, '' # Assuming all can receive notifications for now

def _check_dashboard_viewer(info, kind):
    # Needs a browser.
    return 'browser' in info.get('loaded', []), 'browser not detected'

def _check_storage_cache(info, kind):
    # Needs sufficient disk space.
    return True, '' # Assuming all can store something for now

def _check_ci_runner(info, kind):
    # Tooling for builds/tests.
    return 'git' in info.get('loaded', []), 'git not detected'

def _check_role(role, info, kind='machine'):
    """Checks for each role based on hardware and software capabilities."""
    if role == 'worker':
        return _check_worker(info, kind)
    if role == 'host':
        return _check_host(info, kind)
    if role == 'device_host':
        return _check_device_host(info, kind)
    if role == 'cast_receiver':
        return _check_cast_receiver(info, kind)
    if role == 'test_runner':
        return _check_test_runner(info, kind)
    if role == 'presence_source':
        return _check_presence_source(info, kind)
    if role == 'energy_meter':
        return _check_energy_meter(info, kind)
    if role == 'notification_target':
        return _check_notification_target(info, kind)
    if role == 'dashboard_viewer':
        return _check_dashboard_viewer(info, kind)
    if role == 'storage_cache':
        return _check_storage_cache(info, kind)
    if role == 'ci_runner':
        return _check_ci_runner(info, kind)
    return False, f'unknown role {role}'

