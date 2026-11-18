import yaml
import sys

try:
    with open('/home/sergiu-alexandrescu/Projects/hcm-wt/tutti-run-ap-t213/docs/openapi.yaml', 'r') as f:
        yaml.safe_load(f)
    print("YAML is valid")
except Exception as e:
    print(f"YAML is invalid: {e}")
