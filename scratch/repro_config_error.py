from tutti import config
import os
import tempfile

with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, 'tutti.config.yaml')
    with open(path, 'w', encoding='utf8') as f:
        f.write('machines:\n  a:\n    transport: local\n    slots: 0\n')
    
    config.load(path)
