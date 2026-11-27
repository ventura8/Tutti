"""What Tutti learns from outcomes: a success and failure count per ladder step, kept in the state folder."""
import os

from . import config, routing, telemetry

FILE = 'agent-history.json'


def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def read_history(cfg):
    return telemetry.read_json(path_of(cfg), {})


def cohort(task):
    """A bounded, non-identifying task category for comparing like attempts."""
    from . import fleet
    return f'{fleet.kind_of(task)}:{task.get("size") or "S"}'


def update_counts(entry, success):
    wins, total = routing.agent_counts(entry)
    return {'successes': wins + int(success), 'failures': total - wins + int(not success)}


def categories_of(entry):
    categories = entry.get('categories') if isinstance(entry, dict) else None
    return categories if isinstance(categories, dict) else {}


def record_outcome(cfg, agent, success, category=None):
    """Count one outcome for a ladder step without storing task details."""
    if not agent:
        return
    data = read_history(cfg)
    entry = data.get(agent)
    updated = update_counts(entry, success)
    categories = categories_of(entry)
    if category:
        updated['categories'] = {**categories, category: update_counts(categories.get(category), success)}
    data[agent] = updated
    telemetry.write_json(path_of(cfg), data, mode=0o600)
