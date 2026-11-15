"""The Chat's controls (docs/CHAT.md, *Controls*), pure apart from reading a repository's files: the mode (autonomous, ask
before acting, plan review), the effort and what it means for each model, @-mentions and the context they add, token and
cost accounting with the context share, auto-compaction, images, branches and plans. chatrun.py wires them in."""
import base64
import os
import re
import subprocess

MODES = {'auto': 'Autonomous', 'ask': 'Ask before acting', 'plan': 'Plan review'}
EFFORTS = ('auto', 'low', 'medium', 'high', 'max')
DEFAULTS = {'model': 'auto', 'effort': 'auto', 'mode': 'auto'}
ROUNDS = {'auto': 4, 'low': 2, 'medium': 4, 'high': 6, 'max': 8}   # tool rounds an answer may take
CTX_BASE = 16384
CTX_MAX = 65536
CLI_WINDOW = 200000            # tokens of an online agent's window unless workers.<n>.context_tokens says
CLAUDE_EFFORT = {'low': 'low', 'medium': 'medium', 'high': 'high', 'max': 'max'}
IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/webp', 'image/gif')
MAX_IMAGE_BYTES = 1024 * 1024
MENTION = re.compile(r'@(project|job|machine|file):([^\s]+)')
MENTIONS_SHOWN = 20
FILE_BYTES = 32 * 1024
KEEP_RECENT = 4                # messages a compaction always keeps whole


class ControlError(Exception):
    """A control the chat refuses, in plain words."""


# ---- the conversation's settings: its own, else the user's defaults, else Auto


def user_defaults(data):
    d = dict(DEFAULTS)
    d.update({k: v for k, v in (data.get('defaults') or {}).items() if k in DEFAULTS})
    if data.get('default_model') and 'model' not in (data.get('defaults') or {}):   # before 0.2: the picker's last choice
        d['model'] = data['default_model']
    return d


def resolve(data, conv):
    """{model, effort, mode, compact_pct} of a conversation."""
    d = user_defaults(data)
    out = {k: (conv or {}).get(k) or d[k] for k in DEFAULTS}
    out['compact_pct'] = data.get('compact_pct')
    return out


def check(changes, model_ok):
    """The changes cleaned, or ControlError: a known model, effort and mode; a compaction share from 10 to 95 %."""
    out = {}
    for key, value in (changes or {}).items():
        if value is None:
            continue
        if key == 'model' and not model_ok(str(value)):
            raise ControlError(f'There is no model "{value}" here. Reload the page to see the current list.')
        if key == 'effort' and value not in EFFORTS:
            raise ControlError(f'Effort "{value}" is not one of {", ".join(EFFORTS)}.')
        if key == 'mode' and value not in MODES:
            raise ControlError(f'Mode "{value}" is not one of: {", ".join(MODES)} ({", ".join(MODES.values())}).')
        if key == 'compact_pct' and (isinstance(value, bool) or not isinstance(value, int) or not 10 <= value <= 95):
            raise ControlError('Compact at a whole percentage from 10 to 95.')
        if key in ('model', 'effort', 'mode', 'compact_pct'):
            out[key] = value
    return out


def apply(data, conv, changes):
    """Sets the changes on the conversation (when one is open) and as the user's defaults for new chats."""
    defaults = data.setdefault('defaults', {})
    for key, value in changes.items():
        if key == 'compact_pct':
            data['compact_pct'] = value
            continue
        defaults[key] = value
        if conv is not None:
            conv[key] = value
    data.pop('default_model', None)


# ---- effort: what it means for each kind of model


def ollama_options(worker, effort):
    """(think, num_ctx) for a local model: more effort thinks first and reads a longer context, within the worker's cap."""
    base = int(worker.get('num_ctx') or CTX_BASE)
    cap = int(worker.get('max_ctx') or max(base, CTX_MAX))
    think, ctx = {'auto': (False, base), 'low': (False, min(base, 8192)), 'medium': (False, base),
                  'high': (True, max(base, 32768)), 'max': (True, CTX_MAX)}[effort if effort in EFFORTS else 'auto']
    return think, min(ctx, cap)


def cli_args(worker, effort):
    """Extra arguments for an online agent's effort: `workers.<n>.effort_args` ({effort} in it), else Claude Code's
    `--effort`; an agent without such a setting answers as it always does."""
    if effort == 'auto':
        return []
    tpl = worker.get('effort_args')
    if tpl is None and os.path.basename(str((worker.get('command') or [''])[0])) == 'claude':
        tpl = ['--effort', '{effort}']
    level = CLAUDE_EFFORT.get(effort, effort)
    return [str(a).replace('{effort}', level) for a in tpl or []]


def window(cand, cfg, effort):
    """The tokens a model reads at most: a local model's num_ctx at this effort, an online agent's context window."""
    w = (cfg.get('workers') or {}).get(cand.get('worker')) or {}
    if cand.get('kind') == 'ollama':
        return ollama_options(w, effort)[1]
    return int(w.get('context_tokens') or (cfg.get('autocompact') or {}).get('context_tokens') or CLI_WINDOW)


# ---- tokens, cost and the context share


def tokens(text):
    """A rough count (four characters a token) where the model reports none."""
    return max(1, len(text or '') // 4)


def messages_tokens(messages):
    return sum(tokens(m.get('content') or '') + 4 for m in messages)


def cost_usd(kind, tokens_in, tokens_out, prices=(3.0, 15.0)):
    """What the tokens would cost at an online model's API price (an account's quota is spent, not money); local: 0."""
    if kind == 'ollama':
        return 0.0
    return round(tokens_in / 1e6 * prices[0] + tokens_out / 1e6 * prices[1], 4)


def add_usage(conv, usage):
    """Adds one answer's usage to the conversation's totals and keeps the latest context share."""
    total = conv.setdefault('usage', {'in': 0, 'out': 0, 'usd': 0.0, 'context_pct': 0})
    total['in'] += usage.get('in', 0)
    total['out'] += usage.get('out', 0)
    total['usd'] = round(total['usd'] + usage.get('usd', 0.0), 4)
    total['context_pct'] = usage.get('context_pct', total['context_pct'])
    return total


# ---- auto-compaction


def compact_plan(history, used, win, pct):
    """The messages to fold into a summary when the next request would fill `pct` % of the window, else None. The last
    KEEP_RECENT messages always stay whole."""
    if not win or used * 100 < pct * win or len(history) <= KEEP_RECENT:
        return None
    return history[:-KEEP_RECENT]


def summary_text(old, previous=''):
    """A summary without a model: each folded message's first line, so nothing is lost silently."""
    lines = [previous] if previous else []
    for m in old:
        first = (m.get('text') or '').strip().splitlines()[:1]
        if first:
            lines.append(f'- {"You" if m["role"] == "user" else "Tutti"}: {first[0][:160]}')
    return '\n'.join(lines)


SUMMARY_PROMPT = ('Summarise this conversation so far for yourself, in at most 12 short bullet points: what the user wants, '
                  'decisions, names, ids and open questions. Plain text only.')


# ---- images


def check_image(a):
    """An image attachment cleaned ({name, type, size, data}), or ControlError."""
    kind, data = a.get('type'), a.get('data')
    name = str(a.get('name') or 'image')[:80]
    if kind not in IMAGE_TYPES or not isinstance(data, str):
        raise ControlError(f'"{name}" is not an image this chat takes (PNG, JPEG, WebP or GIF).')
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError as e:
        raise ControlError(f'"{name}" did not arrive whole: paste or attach it again.') from e
    if len(raw) > MAX_IMAGE_BYTES:
        raise ControlError(f'"{name}" is {len(raw) // 1024} KB; images are limited to {MAX_IMAGE_BYTES // 1024} KB. Crop it or '
                           'take a smaller screenshot.')
    return {'name': name, 'type': kind, 'size': len(raw), 'data': data}


# ---- @-mentions


def _repos(cfg):
    from . import config  # config imports chat, which this module serves
    return {n: config.expand(r.get('path') or '') for n, r in ((cfg.get('autopilot') or {}).get('repos') or {}).items()
            if isinstance(r, dict) and r.get('path')}


def repo_files(path, run=subprocess.run):
    """A repository's tracked files (git ls-files), or [] when it cannot be read."""
    try:
        out = run(['git', '-C', path, 'ls-files', '-z'], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [f for f in out.stdout.split('\0') if f] if out.returncode == 0 else []


def viewer_may(role, kind):
    """What a viewer may mention: what the Dashboard tab shows them (projects, jobs' lines, machines); files and job logs
    are for admins."""
    return role == 'admin' or kind != 'file'


def options(cfg, snap, query, run=subprocess.run, role='admin'):
    """What `@query` may stand for: projects, running jobs, machines and (after `project/`, admins) the project's files."""
    q = (query or '').lower()
    repos = _repos(cfg)
    out = [{'kind': 'project', 'name': n, 'label': f'project {n}'} for n in repos]
    out += [{'kind': 'job', 'name': str(j.get('id')), 'label': f'job {str(j.get("title") or j.get("id"))[:60]}'}
            for j in (snap.get('jobs') or {}).get('items') or [] if j.get('status') in ('running', 'stalled')]
    out += [{'kind': 'machine', 'name': n, 'label': f'machine {n}'} for n in cfg.get('machines') or {}]
    if '/' in q and viewer_may(role, 'file'):
        project, _, sub = q.partition('/')
        name = next((n for n in repos if n.lower() == project), None)
        if name:
            files = [f for f in repo_files(repos[name], run) if sub in f.lower()]
            return [{'kind': 'file', 'name': f'{name}/{f}', 'label': f'file {name}/{f}'} for f in files[:MENTIONS_SHOWN]]
    return [o for o in out if q in o['name'].lower() or q in o['label'].lower()][:MENTIONS_SHOWN]


def read_file(cfg, ref, run=subprocess.run):
    """(text, None) of `project/path`, or (None, why): a file git tracks in the repository (never an untracked or ignored
    one), not a secret, text, at most 32 KB."""
    from . import files
    project, _, rel = ref.partition('/')
    root = _repos(cfg).get(project)
    if not root or not rel:
        return None, f'"{ref}" is not a file of a known project (write @file:<project>/<path>)'
    full = os.path.realpath(os.path.join(root, rel))
    if not full.startswith(os.path.realpath(root) + os.sep):
        return None, f'"{ref}" is outside the project'
    if os.path.relpath(full, os.path.realpath(root)) not in set(repo_files(root, run)):
        return None, f'"{ref}" is not a file git tracks in {project} (untracked and ignored files are never read)'
    why = files.secret_reason(full)
    if why:
        return None, f'"{ref}" looks like a secret ({why}), so it is never read'
    try:
        with open(full, 'rb') as f:
            raw = f.read(FILE_BYTES + 1)
    except OSError:
        return None, f'"{ref}" does not exist'
    if b'\x00' in raw:
        return None, f'"{ref}" is not a text file'
    cut = '\n(cut at 32 KB)' if len(raw) > FILE_BYTES else ''
    return raw[:FILE_BYTES].decode('utf8', 'replace') + cut, None


def expand(cfg, snap, text, facts_of, role='admin'):
    """The context the message's @-mentions add, as one block for the model, and the ones that could not be read."""
    parts, missing = [], []
    for kind, name in MENTION.findall(text or ''):
        name = name.rstrip('.,;:!?)')
        if not viewer_may(role, kind):
            missing.append(f'@{kind}:{name} (files are for admins; viewers mention projects, jobs and machines)')
            continue
        if kind == 'file':
            body, why = read_file(cfg, name)
            found = f'File {name}:\n```\n{body}\n```' if body is not None else None
        else:
            found, why = facts_of(kind, name), f'no {kind} "{name}" here'
        if found:
            parts.append(found)
        else:
            missing.append(why)
    return '\n\n'.join(parts), missing


# ---- plans


def plan_card(card_id, calls, describe, at):
    return {'id': card_id, 'tool': 'plan', 'label': f'A plan of {len(calls)} step{"s" if len(calls) != 1 else ""}', 'at': at,
            'state': 'plan', 'steps': [{'id': f's{i + 1}', 'call': c, 'label': describe(c), 'state': 'pending'}
                                       for i, c in enumerate(calls)]}


TEXT_ARGS = ('title', 'task', 'question')   # what the user may edit in a plan's step


def edited_steps(card, edits):
    """The steps to run, in the order given: each kept step with its text edited; a step left out is skipped.
    `edits` = [{id, text?}] or None for the plan as proposed."""
    if edits is None:
        return [dict(s) for s in card['steps']]
    if not isinstance(edits, list) or not all(isinstance(e, dict) for e in edits):
        raise ControlError('Send the plan\'s steps as a list.')
    by_id = {s['id']: s for s in card['steps']}
    out = []
    for e in edits:
        step = by_id.get(e.get('id'))
        if step is None:
            raise ControlError('That plan changed: reload the chat.')
        step = {**step, 'call': {'tool': step['call']['tool'], 'args': dict(step['call']['args'])}}
        text = e.get('text')
        if isinstance(text, str) and text.strip():
            key = next((k for k in TEXT_ARGS if k in step['call']['args']), None)
            if key:
                step['call']['args'][key] = text.strip()[:300]
        out.append(step)
    return out


# ---- Auto: when a local model hands over to an online one (docs/CHAT.md, *Escalation*)

CODE_WORDS = re.compile(r'```|\b(stack ?trace|traceback)\b|\b(write|implement|refactor|debug|fix|review)\s+'
                        r'(?:(?:a|an|the|this|that|my|some|new|unit|failing)\s+){0,2}'   # 'review the test results' is not code work
                        r'(code|function|method|class|script|regex|tests?|bug|query|patch|diff)\b'
                        r'(?!\s+(?:results?|reports?|logs?|runs?|output|summary|status|schedule|plan|times?)\b)', re.I)
CODE_PARAMS = 14.0   # a local model smaller than this is not trusted with code
# only at the very start of a short answer that used no tool: "I can't find any runs today" after a lookup is a real answer
REFUSAL = re.compile(r"^\W*(sorry,?\s*)?(i can(?:no|['’])t (help|do|assist|answer)|i am unable to|i['’]m unable to|as an ai)\b", re.I)
UNSURE = re.compile(r"^\W*(i(?:['’]m| am) not sure|i don['’]t know|i do not know|i['’]m uncertain)\b", re.I)
SHORT = 600
BAD_CALLS = 2


def seen(history, turns):
    """The history messages a model sees (chat.model_messages keeps the last `turns`)."""
    return history[-turns:] if turns else []


def route_reason(cand, cfg, messages, history, effort):
    """Why a local model should not even try this request (Auto only), or None: it would not fit its context, it has an
    image but no vision, it is code work for a small model, or the effort is max."""
    if cand.get('kind') != 'ollama':
        return None
    w = (cfg.get('workers') or {}).get(cand['worker']) or {}
    if messages_tokens(messages) > window(cand, cfg, effort):
        return f'the request is longer than {cand["label"]}\'s context'
    last = history[-1] if history else {}
    if any(a.get('ref') or str(a.get('type') or '').startswith('image/') for a in last.get('attachments') or []) and not w.get('vision'):
        return f'it has an image and {cand["label"]} cannot see images'
    if CODE_WORDS.search(last.get('text') or '') and (cand.get('params') or 0) < CODE_PARAMS:
        return f'it is code work and {cand["label"]} is too small for it'
    if effort == 'max':
        return 'the effort is max'
    return None


def answer_reason(text, bad_calls, used_tools=False):
    """Why a local model's answer is not good enough (Auto retries online), or None. `bad_calls`: refused tool calls in a row."""
    if bad_calls >= BAD_CALLS:
        return 'the local model called its tools wrongly twice in a row'
    text = (text or '').strip()
    if not text:
        return 'the local model gave no answer'
    if used_tools or len(text) > SHORT:
        return None
    if REFUSAL.search(text):
        return 'the local model declined'
    if UNSURE.search(text):
        return 'the local model was not confident'
    return None
