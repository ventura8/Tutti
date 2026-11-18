import os
import tempfile
from tutti import config

# Simulate tests/run.py
os.environ['TUTTI_NO_DAEMONS'] = '1'
# Create a dummy config in the current directory to simulate the problem
with open('tutti.config.yaml', 'w') as f:
    f.write('{}')

# Test finding it
found = config.find()
print(f"Found config: {found}")

# Test with isolation
os.environ['XDG_CONFIG_HOME'] = tempfile.mkdtemp()
found_isolated = config.find()
print(f"Found config isolated: {found_isolated}")
