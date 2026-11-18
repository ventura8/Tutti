import json

from . import remote


def detect_all(cfg):
    """Detects all GPUs and NPUs on all machines."""
    results = {}
    for name, machine in cfg['machines'].items():
        r = remote.sh(machine, f'bash {remote.root_path(machine)}/scripts/detect-devices.sh', capture=True)
        if r.returncode == 0:
            results[name] = parse_output(r.stdout)
    return results


def parse_output(text):
    devices = {'gpu': [], 'npu': []}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            device = json.loads(line)
            kind = device.get('kind')
            if kind == 'gpu':
                devices['gpu'].append(device)
            elif kind == 'npu':
                devices['npu'].append(device)
        except json.JSONDecodeError:
            pass
    return devices


def prioritize(devices):
    """Prioritizes devices based on memory (GPU) and presence (NPU)."""
    # Simple prioritization: NVIDIA > AMD > Intel for GPU, then by memory.
    gpus = sorted(devices.get('gpu', []), key=lambda d: (d.get('vendor') != 'NVIDIA', -int(d.get('memory_mb', 0))))
    return {'gpus': gpus, 'npus': devices.get('npu', [])}
