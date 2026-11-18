import sys
sys.path.insert(0, '/home/sergiu-alexandrescu/Projects/hcm-wt/tutti-run-ap-t235')
from tutti import config
import os

# Create a mock tutti.config.yaml
with open('tutti.config.yaml', 'w') as f:
    f.write("devices:\n  power_on: true\n  myphone:\n    serial: 123\n")

# Load and normalise
raw = config.parse(open('tutti.config.yaml').read())
cfg = config.normalise(raw)
print(f"devices.power_on: {cfg['devices']['power_on']}")
print(f"myphone: {cfg['devices']['myphone']}")
