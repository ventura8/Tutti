from tutti import homeassistant
import json

print(json.dumps(homeassistant.discover({}), indent=2))
