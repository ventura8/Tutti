import re

RATE_RX = re.compile(r'\b429\b|rate[ -]?limit|quota (?:reached|exceeded|exhausted)|usage limit|'
                     r'resource[_ ]exhausted|refused a command|auto-denied|required the .+ permission|the run ended without a report', re.I)

examples = [
    "permission denied",
]

for ex in examples:
    print(f"'{ex}': {bool(RATE_RX.search(ex))}")
