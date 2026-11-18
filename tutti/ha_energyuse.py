"""Home Assistant energy usage monitoring (smart plugs)."""
import json
import os
import time
from . import config, homeassistant

FILE = 'ha-energy-use.json'
KEEP = 24 * 3600  # Keep 24 hours of data

def path(cfg):
    return os.path.join(config.state_dir(cfg), FILE)

def load(cfg):
    try:
        with open(path(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

def record(cfg, now=None):
    """Probes HA energy entities and records them."""
    now = now or time.time()
    energy_cfg = (cfg.get('home_assistant') or {}).get('energy_entities', {})
    data = load(cfg)
    
    for machine, entity_id in energy_cfg.items():
        usage = homeassistant.get_energy_usage(cfg, entity_id)
        if usage is not None:
            machine_data = data.get(machine, [])
            machine_data.append([now, usage])
            # Drop old data
            data[machine] = [p for p in machine_data if p[0] >= now - KEEP]
    
    tmp = f'{path(cfg)}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(data, f)
    os.replace(tmp, path(cfg))
    return data

def view(cfg, now=None):
    """Summarizes HA energy usage."""
    now = now or time.time()
    data = load(cfg)
    results = {}
    for machine, pts in data.items():
        recent = [p[1] for p in pts if p[0] >= now - 3600]
        results[machine] = round(sum(recent) / len(recent), 2) if recent else 0.0
    return results
