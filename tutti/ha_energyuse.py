"""Home Assistant power samples and measured task cost."""
import json
import os
import sys
import time
from contextlib import contextmanager
from threading import Lock

from . import config, durable, energy, ha_energy, homeassistant, tz

FILE = 'ha-energy-use.json'
KEEP = 24 * 3600
_write_lock = Lock()


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
    """Return live watts and keep the samples for calculating task costs."""
    now = time.time() if now is None else now
    samples = ha_energy.probe_all(cfg)
    if not samples:
        return samples
    with _write_lock:
        data = load(cfg)
        for machine, watts in samples.items():
            data[machine] = [p for p in data.get(machine, []) if now - KEEP <= p[0] < now] + [[now, watts]]
        os.makedirs(os.path.dirname(path(cfg)), exist_ok=True)
        durable.dump_json(path(cfg), data)
    return samples


def cost(cfg, points):
    """Trapezoidal integration split across the configured local tariff hours."""
    tariff = energy.tariff(cfg)
    zone = tz.zone(cfg)
    kwh = total = 0.0
    for (start, first), (end, last) in zip(points, points[1:], strict=False):
        at = start
        while at < end:
            local = tz.localtime(at, zone)
            stop = min(end, int(at) + (60 - local.tm_min) * 60 - local.tm_sec)
            avg = first + (last - first) * ((at + stop) / 2 - start) / (end - start)
            used = avg * (stop - at) / 3_600_000
            kwh += used
            total += used * energy.price_at(tariff, local.tm_hour)
            at = stop
    return {'kwh': round(kwh, 6), 'cost': round(total, 6), 'currency': tariff['currency']}


@contextmanager
def measure(cfg, machine, log_path):
    name = config.physical(cfg, machine)
    entity = (homeassistant.settings(cfg).get('energy_entities') or {}).get(name)
    if not entity:
        yield
        return
    first = homeassistant.get_energy_usage(cfg, entity)
    start = time.time()
    try:
        yield
    finally:
        end = time.time()
        last = homeassistant.get_energy_usage(cfg, entity)
        if first is not None and last is not None and end > start:
            points = [(start, first), *sorted(p for p in load(cfg).get(name, []) if start < p[0] < end), (end, last)]
            try:
                durable.dump_json(log_path + '.energy', cost(cfg, points))
            except OSError as e:
                sys.stderr.write(f'tutti: could not save task energy ({e.strerror}): check the state folder permissions\n')


def read_cost(log_path):
    try:
        with open(log_path + '.energy', encoding='utf8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
