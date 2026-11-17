
import os
import json
import sys
from tutti import config, dispatch, shards

# Setup a minimal config
os.makedirs('tmp_state', exist_ok=True)
cfg_path = 'tutti.config.test.yaml'
raw = {
    'project': {'state': 'tmp_state'},
    'machines': {'local': {'transport': 'local'}},
    'projects': {
        'Radio': {
            'tests': {
                'e2e': {
                    'kind': 'e2e',
                    'targets': ['machine'],
                    'run': 'echo "hello"',
                    'prepare': {'run': 'echo "preparing"'}
                }
            }
        }
    }
}
with open(cfg_path, 'w', encoding='utf8') as f:
    json.dump(raw, f)

cfg = config.load(cfg_path)
rc_path = 'tmp_rc.txt'

# Run build
try:
    rc = shards.run_prepare(cfg, 'Radio', 'e2e', rc_path)
    print(f"RC: {rc}")
    with open(rc_path, 'r') as f:
        print(f"File content: {f.read()}")
except Exception as e:
    print(f"Exception: {e}")
