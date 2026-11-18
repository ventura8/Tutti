import json
import os

from tutti import backlog, config

# Load current backlog
cfg = config.load('tutti.config.example.yaml')
backlog_path = os.path.join(config.state_dir(cfg), backlog.FILE)

# Read the data using `backlog.load` to ensure defaults are applied
data = backlog.load(cfg)

# Seed function
def add_or_update(data, tid, repo, title, depends_on=None, together_with=None):
    task = None
    for t in data['tasks']:
        if t['id'] == tid:
            task = t
            break

    if task:
        task['depends_on'] = depends_on or []
        task['together_with'] = together_with or []
    else:
        # Create new task if not found
        task = backlog.new_task(repo, title, depends_on=depends_on, together_with=together_with)
        task['id'] = tid
        data['tasks'].append(task)
        # Update next ID if necessary
        next_id = int(tid[1:]) + 1
        if next_id > data['next']:
            data['next'] = next_id
    return task

# Seeds
# t242 depends on t211, t241, #606, device move (feat/adb-via-device-host)
add_or_update(data, 't242', 'hcm', 'Feature: t242', depends_on=['t211', 't241', 't606', 't_device_move'])

# t199/t200/t201/t202/t187 together (chat)
add_or_update(data, 't199', 'hcm', 'Chat t199', together_with=['t200', 't201', 't202', 't187'])
add_or_update(data, 't200', 'hcm', 'Chat t200', together_with=['t199', 't201', 't202', 't187'])
add_or_update(data, 't201', 'hcm', 'Chat t201', together_with=['t199', 't200', 't202', 't187'])
add_or_update(data, 't202', 'hcm', 'Chat t202', together_with=['t199', 't200', 't201', 't187'])
add_or_update(data, 't187', 'hcm', 'Chat t187', together_with=['t199', 't200', 't201', 't202'])

# t233/t212/t235 together (Home Assistant)
add_or_update(data, 't233', 'hcm', 'Home Assistant t233', together_with=['t212', 't235'])
add_or_update(data, 't212', 'hcm', 'Home Assistant t212', together_with=['t233', 't235'])
add_or_update(data, 't235', 'hcm', 'Home Assistant t235', together_with=['t233', 't212'])

# t181/t185/t239 together (Linear/Slack)
add_or_update(data, 't181', 'hcm', 'Linear/Slack t181', together_with=['t185', 't239'])
add_or_update(data, 't185', 'hcm', 'Linear/Slack t185', together_with=['t181', 't239'])
add_or_update(data, 't239', 'hcm', 'Linear/Slack t239', together_with=['t181', 't185'])

# Save
with open(backlog_path, 'w', encoding='utf8') as f:
    json.dump(data, f, indent=1)
print("Seeded backlog")
