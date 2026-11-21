"""Household decisions (t293): once a person answers a question, Tutti keeps the answer in <state>/decisions.json and never
asks it again: a later question that matches is answered from it (needs.ask), and every worker brief and Chat prompt lists
the decisions so no agent re-asks them in its notes.

A decision matches a question in one of two ways:
- `key`: the question's normalised text (lower case, task and question ids, numbers and punctuation removed), written
  when a person answers a question in the needs queue (Chat, Needs you, `tutti needs answer`, a worker's question);
- `match`: words that must all appear in the question, for a decision written by hand (`tutti decisions add`).

Answers that only steer one parked task (retry, retry-from-scratch, drop, done, later) are not decisions: the same
"keeps failing" question can need a different answer next time."""
import json
import os
import re
import time

from . import config, durable

FILE = 'decisions.json'
TASK_ONLY = frozenset(('retry', 'retry-from-scratch', 'drop', 'done', 'later', 'yes', 'no'))
ID_RX = re.compile(r'\b[tqs]\d+\b|#\d+|\d+')
WORD_RX = re.compile(r'[a-z][a-z0-9+.-]*')
MAX_BRIEF = 20


def path_of(cfg):
    return os.path.join(config.state_dir(cfg), FILE)


def normalise(text):
    """The question without ids, numbers or punctuation, in lower case: the same question asked again has the same key."""
    return ' '.join(WORD_RX.findall(ID_RX.sub(' ', str(text or '').lower())))


def load(cfg):
    try:
        with open(path_of(cfg), encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    items = data.get('decisions') if isinstance(data, dict) else None
    return [d for d in items or [] if isinstance(d, dict) and d.get('answer')]


def save(cfg, items):
    path = path_of(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    durable.dump_json(path, {'decisions': items})


def matches(d, question):
    norm = normalise(question)
    if d.get('key'):
        return d['key'] == norm
    words = [normalise(w) for w in d.get('match') or []]
    return bool(words) and all(w and w in norm for w in words)


def find(cfg, question, items=None):
    """The newest decision that answers `question`, or None."""
    return next((d for d in reversed(load(cfg) if items is None else items) if matches(d, question)), None)


def is_decision(answer):
    """True when an answer settles a question for good, not just one parked task's next step."""
    a = str(answer or '').strip().lower()
    return bool(a) and a not in TASK_ONLY


def record(cfg, question, answer, by='owner', source='needs', now=None):
    """Keeps a person's answer; returns the decision, or None when the answer only steered one task."""
    if not is_decision(answer):
        return None
    key = normalise(question)
    if not key:
        return None
    items = [d for d in load(cfg) if d.get('key') != key]
    d = {'id': next_id(items), 'key': key, 'question': str(question)[:300], 'answer': str(answer)[:300], 'by': by,
         'source': source, 'at': now or time.time()}
    save(cfg, [*items, d])
    return d


def add(cfg, match, answer, note='', by='owner', now=None):
    """A decision written by hand: `match` words that must all appear in a question, and its answer."""
    words = [w.strip() for w in match if w.strip()]
    if not words or not str(answer).strip():
        raise ValueError('a decision needs at least one match word and an answer: tutti decisions add "<words>" "<answer>"')
    items = load(cfg)
    d = {'id': next_id(items), 'match': words, 'answer': str(answer)[:300], 'note': str(note)[:300], 'by': by, 'source': 'cli',
         'at': now or time.time()}
    save(cfg, [*items, d])
    return d


def remove(cfg, did):
    items = load(cfg)
    kept = [d for d in items if d.get('id') != did]
    if len(kept) == len(items):
        return False
    save(cfg, kept)
    return True


def next_id(items):
    nums = [int(d['id'][1:]) for d in items if str(d.get('id', '')).startswith('d') and str(d['id'][1:]).isdigit()]
    return f'd{max(nums, default=0) + 1}'


def line(d):
    what = d.get('question') or ' + '.join(d.get('match') or [])
    return f'{what[:160]} -> {d["answer"]}' + (f' ({d["note"]})' if d.get('note') else '')


def brief_lines(cfg):
    """The decisions a worker or Chat model must follow instead of asking again (newest first, bounded)."""
    items = load(cfg)[-MAX_BRIEF:]
    if not items:
        return []
    return ['', 'Household decisions already made (follow them; never ask the owner these again):'] + \
        [f'- {line(d)}' for d in reversed(items)]
