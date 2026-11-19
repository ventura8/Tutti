"""Chat token efficiency (docs/CHAT.md, *Token efficiency*): minimal per-turn prompts, lazy tool schemas, cheapest-first
step routing, bounded fresh history, prompt-cache prefixes, and a fixed-task benchmark against desktop apps."""

import json
import re

# Desktop apps ship a large always-on system prompt and every tool schema on every turn.
DESKTOP_SYSTEM_TOKENS = 12000
DESKTOP_TOOL_TOKENS = 8000
FRESH_BOUND = 6                 # recent messages a worker sees (older ones are compacted)
DEFAULT_LOCAL_WATTS = 150.0

READ_TOOLS = frozenset({'usage', 'machines', 'devices', 'job_status', 'backlog_list'})
WORK_TOOLS = frozenset({'backlog_add', 'delegate_task', 'test_on', 'scout_ask', 'control'})
ALL_TOOLS = READ_TOOLS | WORK_TOOLS

STATUS_RE = re.compile(r'\b(quota|running|idle|what(?:\'s| is) waiting|how much|what did i miss)\b', re.I)
WORK_RE = re.compile(r'\b(add task|delegate|pause|resume|scout|queue tests|ask )\b', re.I)
CODE_RE = re.compile(r'```|\b(write|implement|refactor|debug|fix|review)\s+'
                     r'(?:(?:a|an|the|this|that|my|some|new|unit|failing)\s+){0,2}'
                     r'(code|function|method|class|script|tests?|bug|patch)\b', re.I)
READ_RE = re.compile(r'\b(machines?|devices?|backlog|job[s _-]?status|what(?:\'s| is) running)\b', re.I)

TASKS = (
    {'id': 'quota', 'text': 'How much quota is left?', 'kind': 'status'},
    {'id': 'idle', 'text': 'Why is the NUC idle?', 'kind': 'status'},
    {'id': 'running', 'text': "What's running?", 'kind': 'read'},
    {'id': 'queue', 'text': 'add task in map: fix the legend on phones', 'kind': 'work'},
    {'id': 'delegate', 'text': 'delegate in map: write a unit test for the legend', 'kind': 'code'},
)

SAMPLE_TOOLS = {n: {'description': n, 'schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
                    'mutating': n not in READ_TOOLS} for n in sorted(ALL_TOOLS)}

_MINIMAL = """You are Tutti's household assistant. Answer briefly in Markdown from the FACTS. Tools: {tools}.
Call a tool only with names that appear here. Never invent jobs, numbers or names missing from the FACTS.
{protocol}Projects: {projects}. Scouts: {scouts}. Machines: {machines}. Devices: {devices}.
The user is {who} ({role}).

FACTS
{facts}"""

_ACTION = ('To call a tool, write one line: ACTION {"tool": "<name>", "args": {...}}. Schemas: ')


def tokens(text):
    """Rough count (four characters a token), matching chatctl.tokens."""
    return max(1, len(text or '') // 4) if text else 0


def step_kind(text):
    """Which tools this turn needs: status, read, work, code or general."""
    t = text or ''
    if CODE_RE.search(t):
        return 'code'
    if WORK_RE.search(t):
        return 'work'
    if READ_RE.search(t):
        return 'read'
    if STATUS_RE.search(t):
        return 'status'
    return 'general'


def tools_for_step(kind):
    """Tool names for a step kind (lazy schemas: only what this turn needs)."""
    if kind == 'status':
        return frozenset({'usage', 'machines', 'job_status'})
    if kind == 'read':
        return READ_TOOLS
    if kind == 'work':
        return ALL_TOOLS
    if kind == 'code':
        return READ_TOOLS | frozenset({'delegate_task', 'test_on', 'backlog_add'})
    # unclear intent: the model may still propose work from plain words (docs/CHAT.md)
    return ALL_TOOLS


def lazy_tools(all_tools, kind):
    """Subset of all_tools for this step; missing names are skipped."""
    keep = tools_for_step(kind)
    return {n: all_tools[n] for n in keep if n in all_tools}


def ollama_tools_lazy(all_tools, kind):
    """Ollama/OpenAI function-calling list for the step's tools only."""
    return [{'type': 'function', 'function': {'name': n, 'description': t.get('description') or n,
                                              'parameters': t.get('schema') or {'type': 'object'}}}
            for n, t in lazy_tools(all_tools, kind).items()]


def schema_tokens(tools_dict):
    return tokens(json.dumps({n: t.get('schema') for n, t in (tools_dict or {}).items()}, sort_keys=True))


def prompt_tokens(system_text, messages):
    return tokens(system_text) + sum(tokens(m.get('content') or '') + 4 for m in messages or [])


def minimal_system(kind, *, who, role, projects, scouts, machines, devices, facts, tool_names=None, native_tools=True,
                   tools_dict=None):
    """Short system prompt: tool names only when the model has native tools; compact schemas otherwise."""
    names = list(tool_names) if tool_names is not None else sorted(tools_for_step(kind))
    protocol = ''
    if not native_tools:
        schemas = tools_dict if tools_dict is not None else {n: SAMPLE_TOOLS[n]['schema'] for n in names if n in SAMPLE_TOOLS}
        protocol = _ACTION + json.dumps(schemas, sort_keys=True) + '\n'
    return _MINIMAL.format(tools=', '.join(names) or 'none', protocol=protocol, projects=projects or 'none',
                           scouts=scouts or 'none', machines=machines or 'none', devices=devices or 'none',
                           who=who or 'the owner', role=role or 'admin', facts=facts or '(none)')


def desktop_baseline(task_id):
    """Tokens a desktop app would send for a fixed task (always-on system + every tool schema + the user line)."""
    task = next((t for t in TASKS if t['id'] == task_id), None)
    if task is None:
        raise KeyError(f'unknown benchmark task "{task_id}"')
    user = tokens(task['text']) + 4
    total = DESKTOP_SYSTEM_TOKENS + DESKTOP_TOOL_TOKENS + user
    return {'claude-desktop': total, 'cursor': total}


def _tutti_tokens(task):
    system = minimal_system(task['kind'], who='owner', role='admin', projects='map', scouts='grok', machines='nuc, pc',
                            devices='none', facts='(household snapshot)', tool_names=sorted(tools_for_step(task['kind'])),
                            native_tools=True)
    return prompt_tokens(system, [{'role': 'user', 'content': task['text']}]) + schema_tokens(
        lazy_tools(SAMPLE_TOOLS, task['kind']))


def run_benchmark():
    """Fixed-task regression: Tutti's prompt size vs Claude Desktop and Cursor for the same texts."""
    rows, tutti_total, desk = [], 0, {'claude-desktop': 0, 'cursor': 0}
    for task in TASKS:
        tt = _tutti_tokens(task)
        base = desktop_baseline(task['id'])
        win = tt < base['claude-desktop'] and tt < base['cursor']
        rows.append({'id': task['id'], 'text': task['text'], 'kind': task['kind'], 'tutti_tokens': tt,
                     'desktop': base, 'win': win})
        tutti_total += tt
        desk['claude-desktop'] += base['claude-desktop']
        desk['cursor'] += base['cursor']
    beats = all(r['win'] for r in rows) and tutti_total < desk['claude-desktop'] and tutti_total < desk['cursor']
    return {'tasks': rows, 'tutti_total': tutti_total, 'desktop_total': desk, 'beats_desktop': beats}


def message_power(kind_worker, seconds, watts=None):
    """Per-answer power: local models use machine watts; online CLIs use none on this host."""
    secs = max(0.0, float(seconds or 0))
    if kind_worker == 'ollama':
        w = float(DEFAULT_LOCAL_WATTS if watts is None else watts)
    else:
        w = 0.0
    return {'watts': round(w, 1), 'wh': round(w * secs / 3600.0, 4), 'seconds': round(secs, 2)}


def cache_prefix(system_text):
    """Stable system prefix for prompt caching (providers that support it reuse this block)."""
    return system_text or ''


def fresh_history(history, bound=FRESH_BOUND):
    """Bounded fresh worker context: only the last `bound` messages."""
    return list(history or [])[-max(0, int(bound)):]


def step_route_order():
    """Cheapest capable worker first: local Ollama, then agy, cursor-agent, then the main agent."""
    return ('ollama', 'agy', 'cursor', 'main')


def cli_route_rank(worker_name):
    """Sort key for online CLI workers (lower is cheaper / preferred)."""
    n = (worker_name or '').lower()
    if n.startswith('agy') or n.startswith('antigravity'):
        return 0
    if 'cursor' in n:
        return 1
    return 2


def format_message_usage(usage):
    """Short per-message line: tokens, watts, energy, cost."""
    u = usage or {}
    cost = u.get('usd') or 0
    cost_s = 'local: no cost' if cost <= 0 else f'${cost if cost >= 0.01 else "<0.01"}'
    watts = u.get('watts')
    wh = u.get('wh')
    power = f' · {watts:g} W · {wh:g} Wh' if watts is not None and wh is not None else ''
    return f'{u.get("in") or 0} in · {u.get("out") or 0} out{power} · {cost_s}'


def benchmark_banner(result=None):
    """One line for the Chat empty state from run_benchmark()."""
    r = result if result is not None else run_benchmark()
    if r.get('beats_desktop'):
        return (f'Token check: Tutti {r["tutti_total"]} tokens vs Claude Desktop '
                f'{r["desktop_total"]["claude-desktop"]} / Cursor {r["desktop_total"]["cursor"]} on the fixed tasks.')
    return 'Token check: Tutti does not yet beat desktop apps on the fixed benchmark.'
