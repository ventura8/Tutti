import re

ENV_SIDE = ('the worker hit its usage limit', 'the worker\'s CLI refused a command', 'the run ended without a report')
RATE_RX = re.compile(r'\b429\b|rate[ -]?limit|quota (?:reached|exceeded|exhausted)|usage limit|'
                     r'resource[_ ]exhausted|refused a command|auto-denied|required the .+ permission|the run ended without a report', re.I)

def classify(tail):
    if RATE_RX.search(tail):
        if re.search(r'\b429\b|rate[ -]?limit|quota|usage limit|resource[_ ]exhausted', tail, re.I):
            return 'the worker hit its usage limit'
        if re.search(r'refused a command|auto-denied|permission', tail, re.I):
            return 'the worker\'s CLI refused a command'
        if re.search(r'the run ended without a report', tail, re.I):
            return 'the run ended without a report'
    return None

print(classify("You've hit your usage limit"))
print(classify("tool read_file auto-denied"))
print(classify("the run ended without a report"))
