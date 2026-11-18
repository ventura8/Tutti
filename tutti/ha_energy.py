"""Home Assistant energy monitoring."""
from . import homeassistant

def probe_all(cfg):
    """Probes all configured energy entities in Home Assistant."""
    energy_cfg = (cfg.get('home_assistant') or {}).get('energy_entities', {})
    results = {}
    for machine, entity_id in energy_cfg.items():
        usage = homeassistant.get_energy_usage(cfg, entity_id)
        if usage is not None:
            results[machine] = usage
    return results
