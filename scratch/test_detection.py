
import re

NEEDS_PLAN = re.compile(r'(design|refactor|architecture|plan|implement|new feature)', re.I)

def test_detection(text):
    return bool(NEEDS_PLAN.search(text))

test_cases = [
    "design a new system",
    "refactor the chat code",
    "implement a new feature in chat",
    "what is the time?",
    "add task in project: test"
]

for t in test_cases:
    print(f"'{t}': {test_detection(t)}")
