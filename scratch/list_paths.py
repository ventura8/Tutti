import yaml
with open('/home/sergiu-alexandrescu/Projects/hcm-wt/tutti-run-ap-t213/docs/openapi.yaml', 'r') as f:
    data = yaml.safe_load(f)
    print(list(data['paths'].keys()))
