import platform
import re
import shutil
import subprocess

# Platform detection commands
# Linux: powerprofilesctl, tuned, platform_profile, upower
LINUX_MODE_CMD = (
    "if command -v powerprofilesctl >/dev/null 2>&1; then powerprofilesctl get; "
    "elif command -v tuned-adm >/dev/null 2>&1; then tuned-adm active; "
    "elif [ -f /sys/firmware/acpi/platform_profile ]; then cat /sys/firmware/acpi/platform_profile; "
    "elif command -v upower >/dev/null 2>&1; then upower -i $(upower -e | grep 'BAT') | grep 'state' | awk '{print $2}'; "
    "else echo 'unknown'; fi"
)

# macOS: pmset
MACOS_MODE_CMD = "pmset -g | grep 'lowpowermode' | awk '{print $2}' && pmset -g batt | grep -q 'AC Power' && echo 'AC' || echo 'Battery'"

# Windows: powercfg
WINDOWS_MODE_CMD = "powercfg /getactivescheme"

def get_platform():
    return platform.system().lower()

def run_cmd(argv):
    try:
        # Use shell=True to run complex shell commands
        return subprocess.run(argv, capture_output=True, text=True, check=True, shell=True, timeout=5).stdout.strip()
    except Exception:
        return None

def detect_windows():
    out = run_cmd(WINDOWS_MODE_CMD)
    if not out:
        return {'mode': 'unknown', 'source': 'powercfg'}
    # Power Scheme GUID: ... (Name)
    match = re.search(r'\((.*)\)', out)
    if match:
        return {'mode': match.group(1), 'source': 'powercfg'}
    return {'mode': out, 'source': 'powercfg'}

def detect_macos():
    out = run_cmd(MACOS_MODE_CMD)
    if not out:
        return {'mode': 'unknown', 'source': 'pmset'}
    lines = out.split('\n')
    lpm = lines[0] if len(lines) > 0 else 'unknown'
    batt = lines[1] if len(lines) > 1 else 'unknown'
    # lpm is 0 or 1
    lpm_str = 'On' if lpm == '1' else 'Off'
    return {'mode': f'LPM={lpm_str}, {batt}', 'source': 'pmset'}

def detect_linux():
    mode = run_cmd(LINUX_MODE_CMD)
    return {'mode': mode or 'unknown', 'source': 'linux_power_utils'}

def detect_local():
    system = get_platform()
    if system == 'windows':
        return detect_windows()
    elif system == 'darwin':
        return detect_macos()
    elif system == 'linux':
        return detect_linux()
    return {'mode': 'unsupported', 'source': 'none'}

def detect_remote(machine):
    from . import remote
    # Assuming ssh transport for now
    if machine.get('transport') == 'ssh' and machine.get('host'):
        try:
            # remote.SSH is a list of args
            argv = [*remote.SSH, *remote.QUICK, machine['host'], LINUX_MODE_CMD]
            result = subprocess.run(argv, capture_output=True, text=True, check=True, timeout=5)
            return {'mode': result.stdout.strip(), 'source': 'ssh/linux_power_utils'}
        except Exception:
            return {'mode': 'unknown', 'source': 'ssh'}
    return {'mode': 'unknown', 'source': 'none'}

def detect_android(machine):
    try:
        # Check if adb is available
        if not shutil.which('adb'):
            return {'mode': 'adb not found', 'source': 'adb'}

        result = subprocess.run(['adb', 'shell', 'dumpsys', 'battery'], capture_output=True, text=True, check=True, timeout=5)
        # Parse battery status
        charging_check = ['AC powered: true', 'USB powered: true', 'Wireless powered: true']
        is_charging = any(c in result.stdout for c in charging_check)
        is_battery_saver = 'Battery saver: true' in result.stdout

        is_ac = 'AC' if is_charging else 'Battery'
        is_saver = ', Battery Saver' if is_battery_saver else ''
        mode = f'{is_ac}{is_saver}'
        return {'mode': mode, 'source': 'adb'}
    except Exception:
        return {'mode': 'unknown', 'source': 'adb'}

def get_power_mode(machine=None):
    if machine and machine.get('transport') == 'android':
        return detect_android(machine)
    if machine and machine.get('transport') != 'local':
        return detect_remote(machine)
    return detect_local()
