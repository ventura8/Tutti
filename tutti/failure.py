"""Failure classification for Tutti to learn from mistakes."""
import json
import os
import time

from . import brain, config

# Categories
CATEGORIES = [
    'wrong model/agent for the task',
    'too big for it',
    'environment',
    'flaky',
    'the task itself'
]

def classify(cfg, worker, task, rc, lines):
    """Classify the failure of a task using the local main agent."""
    # Simplified classification to keep cyclomatic complexity low
    prompt = f"Task failed (RC {rc}). Task: {task[:50]}. Classify: {', '.join(CATEGORIES)}. Return only the category."
    answer, _ = brain.ask(cfg, prompt, purpose='self-improve')
    
    cat = answer.strip() if answer else 'unknown'
    return cat if cat in CATEGORIES else 'unknown'

def log_failure(cfg, worker, task, rc, category, model=None):
    """Log the failure with its classification."""
    log_path = os.path.join(config.state_dir(cfg), 'failure-log.jsonl')
    entry = {'at': time.time(), 'worker': worker, 'task': task, 'rc': rc, 'category': category, 'model': model}
    with open(log_path, 'a', encoding='utf8') as f:
        f.write(json.dumps(entry) + '\n')
    if category == 'wrong model/agent for the task' and model:
        mark_bad(cfg, model)

def mark_bad(cfg, model):
    path = os.path.join(config.state_dir(cfg), 'bad-models.json')
    bad = []
    if os.path.exists(path):
        with open(path, encoding='utf8') as f:
            bad = json.load(f)
    if model not in bad:
        bad.append(model)
        with open(path, 'w', encoding='utf8') as f:
            json.dump(bad, f)
