import subprocess


def scan(cfg):
    """
    Scans the network for devices using various methods.
    Returns a list of dicts: [{'name': ..., 'type': ..., 'roles': ..., 'ip': ...}, ...]
    """
    discovered = []

    # 1. ARP/neighbour table (fast)
    discovered.extend(_scan_arp())

    # 2. ADB devices (if enabled)
    discovered.extend(_scan_adb())

    # 3. mDNS (if available)
    discovered.extend(_scan_mdns())

    # Merge devices by IP or name if needed.
    # For now, simple deduplication.
    unique_devices = {}
    for d in discovered:
        key = d.get('ip') or d.get('name')
        if key in unique_devices:
            # Merge roles
            unique_devices[key]['roles'] = list(set(unique_devices[key]['roles'] + d.get('roles', [])))
        else:
            unique_devices[key] = d

    # Filter out already configured devices
    configured = set(cfg.get('devices', {}).keys())
    # Also check if it's a machine
    configured_machines = set(cfg.get('machines', {}).keys())

    return [d for d in unique_devices.values() if d.get('name') and d['name'] not in configured and d['name'] not in configured_machines]


def _scan_arp():
    # Use 'ip neigh' on Linux
    try:
        result = subprocess.run(['ip', 'neigh'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            devices = []
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[2] == 'REACHABLE':
                    # parts[0] is IP, parts[2] is state, parts[4] is MAC (if available)
                    devices.append({'name': parts[0], 'type': 'computer', 'roles': [], 'ip': parts[0]})
            return devices
    except Exception:
        pass
    return []


def _scan_adb():
    # 'adb devices'
    try:
        result = subprocess.run(['adb', 'devices'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            devices = []
            for line in result.stdout.splitlines()[1:]:
                if '\t' in line:
                    serial, status = line.split('\t')
                    if status == 'device':
                        devices.append({'name': serial, 'type': 'android', 'roles': ['adb'], 'ip': serial})
            return devices
    except Exception:
        pass
    return []


def _scan_mdns():
    # 'avahi-browse'
    try:
        result = subprocess.run(['avahi-browse', '-rt', '_services._dns-sd._udp'], capture_output=True, text=True, timeout=5)
        devices = []
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith('=') and 'IPv4' in line:
                    parts = line.split(';')
                    # parts: ['=', 'IPv4', 'interface', 'protocol', 'name', 'service_type', 'domain']
                    if len(parts) > 5:
                        name = parts[4]
                        service = parts[5]
                        roles = []
                        if '_ssh' in service:
                            roles.append('ssh')
                        elif '_workstation' in service:
                            roles.append('workstation')

                        devices.append({'name': name, 'type': 'mDNS', 'roles': roles, 'ip': name})
        return devices
    except Exception:
        pass
    return []
