
import json
from datetime import datetime

def generate_plan(task):
    # This is a mock function, in reality it should call the model
    return {
        "goal": f"To complete: {task}",
        "approach": "Step-by-step implementation.",
        "risks": ["Risk 1", "Risk 2"],
        "steps": [
            {"step": "Step 1", "size": "S"},
            {"step": "Step 2", "size": "M"}
        ]
    }

print(json.dumps(generate_plan("design a new database schema"), indent=2))
