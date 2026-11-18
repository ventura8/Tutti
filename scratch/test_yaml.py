import yaml
from tutti import config

try:
    config.parse('machines: : : :')
except Exception as e:
    print(f'Caught: {type(e).__name__}: {e}')
else:
    print('No exception')
