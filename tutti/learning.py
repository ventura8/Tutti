import json
import os
from . import config, telemetry

FILE = 'agent-history.json'

def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)

def read_history(cfg):
    return telemetry.read_json(path_of(cfg), {})

def write_history(cfg, data):
    telemetry.write_json(path_of(cfg), data)

def record_outcome(cfg, agent, task_id, success, cost=0):
    """Records an outcome for an agent."""
    data = read_history(cfg)
    agent_data = data.setdefault(agent, {'successes': 0, 'failures': 0, 'total_cost': 0})
    if success:
        agent_data['successes'] += 1
    else:
        agent_data['failures'] += 1
    agent_data['total_cost'] += cost
    write_history(cfg, data)
