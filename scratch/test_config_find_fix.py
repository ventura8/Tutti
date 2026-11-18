import os
import tempfile
from tutti import config

# Simulate tests/run.py
os.environ['TUTTI_NO_DAEMONS'] = '1'
# Create a dummy config in the current directory
with open('tutti.config.yaml', 'w') as f:
    f.write('{}')

# Simulate the isolation in tests/run.py
os.environ['XDG_CONFIG_HOME'] = tempfile.mkdtemp()

# Current finding
found = config.find()
print(f"Found config in cwd: {found}")

# Simulate changing cwd to a temp folder
os.chdir(tempfile.mkdtemp())
found_after_chdir = config.find()
print(f"Found config after chdir: {found_after_chdir}")
