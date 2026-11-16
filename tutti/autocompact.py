"""Automatic compaction of a worker agent's context at `autocompact.threshold_pct` of its window (60 % by default).

Each worker kind gets its own way, the native one where its CLI has it:
- Claude Code (`claude`): native. The run gets CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=<pct> in its environment, so Claude Code
  compacts by itself at that share of its window.
- a local model in `tutti conduct` (kind ollama): Tutti holds the conversation, so it compacts it itself: the older
  exchanges become one short summary (the commands run and the first line of each answer); the goal and the newest
  exchanges stay.
- every other CLI (cursor-agent, agy, copilot): no headless compaction (their /compact is interactive only). When the run's
  output shows its context use, Tutti stops it at the threshold and starts it again in the same worktree with a handoff
  note (the changes, the last output): the path the dashboard's "stop now" hand-over takes. A CLI that never prints its
  token counts cannot be measured, so it runs as before.

The threshold is the config's `autocompact.threshold_pct`; the dashboard and its Settings tab write that key into the
config file. A value kept by an older dashboard, or for a config it cannot write, in `controls.json`
(settings.autocompact_pct) wins until the key is saved. Pure, apart from reading that file."""
import json
import os
import re

from . import compact, config, controls

DEFAULT_WINDOW = 200_000   # tokens, when neither workers.<w>.context_tokens nor autocompact.context_tokens says
MIN_PCT, MAX_PCT = 10, 95
NATIVE_ENV = {'claude': 'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE'}   # program name -> the variable that sets its threshold
RESTARTS = 3   # a run is restarted with a handoff at most this many times
KEEP_EXCHANGES = 2   # a local model keeps its newest exchanges whole


def settings(cfg):
    return cfg.get('autocompact') or {}


def threshold(cfg, folder=None):
    """The percentage of the window at which a worker's context is compacted: the dashboard's value, else the config's."""
    try:
        chosen = controls.read(folder or config.state_dir(cfg)).get('settings', {}).get('autocompact_pct')
    except OSError:
        chosen = None
    if valid_pct(chosen):
        return chosen
    pct = settings(cfg).get('threshold_pct', 60)
    return pct if valid_pct(pct) else 60


def valid_pct(v):
    return isinstance(v, int) and not isinstance(v, bool) and MIN_PCT <= v <= MAX_PCT


def enabled(cfg):
    return settings(cfg).get('enabled', True) is not False


def window(cfg, worker=None):
    """The worker's context window in tokens."""
    own = (worker or {}).get('context_tokens')
    return own or settings(cfg).get('context_tokens') or DEFAULT_WINDOW


def program(worker):
    cmd = (worker or {}).get('command') or []
    return os.path.basename(str(cmd[0])) if cmd else ''


def mode(worker):
    """'native' (the CLI compacts by itself at a threshold Tutti sets), 'tutti' (Tutti holds the conversation and compacts
    it: a local model) or 'handover' (Tutti stops and restarts the run with a handoff note)."""
    if (worker or {}).get('kind') == 'ollama':
        return 'tutti'
    return 'native' if program(worker) in NATIVE_ENV else 'handover'


def env_for(cfg, worker, folder=None):
    """The environment a worker's run gets for its native compaction ({} when it has none or autocompact is off)."""
    if not enabled(cfg) or mode(worker) != 'native':
        return {}
    return {NATIVE_ENV[program(worker)]: str(threshold(cfg, folder))}


def percent(context_tokens, cfg, worker=None):
    """The context use in % of the worker's window, or None when it is not known."""
    if not context_tokens:
        return None
    return round(100.0 * float(context_tokens) / float(window(cfg, worker)), 1)


# What a CLI prints about its context: "context: 123,456 tokens", "Context 61%", "context window 45 % used".
CONTEXT_PCT = re.compile(r'\bcontext\b[^\n%]{0,30}?(\d{1,3}(?:\.\d+)?)\s*%', re.I)
CONTEXT_TOKENS = re.compile(r'\bcontext\b\W{0,4}(\d[\d,.]*)\s*([kKmM]?)\s*tokens', re.I)
TOKENS_NUM = r'(\d[\d,.]*)\s*([kKmM]?)'
CURSOR_TOKENS_IN = re.compile(TOKENS_NUM + r'\s+(?:input|prompt)\s+tokens|"input_tokens"\s*:\s*(\d+)', re.I)
CURSOR_TOKENS_PAIR = re.compile(r'\btokens?\b\W{0,4}' + TOKENS_NUM + r'\s+in\b', re.I)


def _token_keys(data, keys):
    if not isinstance(data, dict):
        return 0
    return sum(int(data[k]) for k in keys if isinstance(data.get(k), (int, float)) and not isinstance(data.get(k), bool))


def _int_val(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _direct_context(data):
    for k in ('context_tokens', 'context', 'prompt_tokens', 'input_tokens'):
        v = data.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    return None


def _tokens_dict_val(toks):
    if not isinstance(toks, dict):
        return _int_val(toks)
    direct = _direct_context(toks)
    pair = _token_keys(toks, ('input', 'cache_read'))
    return direct if direct is not None else (pair or None)


def _dict_usage_tokens(data):
    usage = data.get('usage') if isinstance(data, dict) else None
    src = usage if isinstance(usage, dict) else data
    val = _token_keys(src, ('input_tokens', 'prompt_tokens', 'cache_creation_input_tokens',
                            'cache_read_input_tokens', 'cache_read_tokens'))
    return val if val > 0 else None


def _agy_data_tokens(data):
    if not isinstance(data, dict):
        return None
    direct = _direct_context(data)
    if direct is not None:
        return direct
    toks = _tokens_dict_val(data.get('tokens'))
    return toks if toks is not None else _dict_usage_tokens(data)


def _read_file_safe(path):
    try:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return None
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except (OSError, ValueError, TypeError):
        return None


def _load_file_payload(source):
    if '\n' not in source and (os.path.isfile(source) or source.endswith(('.json', '.jsonl', '.log'))):
        return _read_file_safe(source)
    return source


def _parse_json_dict(text):
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


def _parse_jsonl_tokens(text, extract_fn):
    last = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        d = _parse_json_dict(line)
        tok = extract_fn(d) if d else None
        if tok is not None:
            last = tok
    return last


def _parse_payload_tokens(text, extract_fn):
    trimmed = text.strip()
    if not trimmed:
        return None
    try:
        data = json.loads(trimmed)
    except (ValueError, UnicodeDecodeError):
        return _parse_jsonl_tokens(trimmed, extract_fn)
    if isinstance(data, list):
        toks = (extract_fn(x) for x in reversed(data))
        return next((t for t in toks if t is not None), None)
    return extract_fn(data)


def agy_session_tokens(source):
    """Context tokens parsed from an agy local session file (path, content or data); None if unreadable."""
    if isinstance(source, dict):
        return _agy_data_tokens(source)
    if isinstance(source, list):
        toks = (_agy_data_tokens(x) for x in reversed(source))
        return next((t for t in toks if t is not None), None)
    if not isinstance(source, str):
        return None
    raw = _load_file_payload(source)
    return _parse_payload_tokens(raw, _agy_data_tokens) if raw else None


def agy_session_percent(path, cfg, worker=None):
    """Context use of an agy session in % of the model's context window, or None."""
    tokens = agy_session_tokens(path)
    return percent(tokens, cfg, worker) if tokens is not None else None


def _cursor_data_tokens(data):
    if not isinstance(data, dict):
        return None
    direct = _direct_context(data)
    if direct is not None:
        return direct
    toks = _tokens_dict_val(data.get('tokens'))
    return toks if toks is not None else _dict_usage_tokens(data)


def _scale_num(num_str, unit):
    try:
        n = float(num_str.replace(',', ''))
        return int(n * {'k': 1e3, 'm': 1e6}.get((unit or '').lower(), 1))
    except (ValueError, TypeError, AttributeError):
        return None


def _cursor_text_tokens(line):
    if not isinstance(line, str):
        return None
    m = CURSOR_TOKENS_PAIR.search(line)
    if m:
        return _scale_num(m.group(1), m.group(2))
    m = CURSOR_TOKENS_IN.search(line)
    if m:
        return int(m.group(3)) if m.group(3) else _scale_num(m.group(1), m.group(2))
    return None


def _cursor_json_line(trimmed):
    d = _parse_json_dict(trimmed)
    return _cursor_data_tokens(d) if d else None


def _cursor_line_tokens(line):
    trimmed = line.strip() if isinstance(line, str) else ''
    if trimmed.startswith('{') and trimmed.endswith('}'):
        return _cursor_json_line(trimmed)
    return _cursor_text_tokens(trimmed) if trimmed else None


def _cursor_lines_tokens(lines):
    toks = (_cursor_line_tokens(x) for x in reversed(lines))
    return next((t for t in toks if t is not None), None)


def _raw_json_dict(raw):
    s = raw.strip() if isinstance(raw, str) else ''
    return _parse_json_dict(s) if s.startswith('{') and s.endswith('}') else None


def cursor_agent_tokens(source):
    """Context tokens extracted from cursor-agent log output or state file; None if unreadable."""
    if isinstance(source, dict):
        return _cursor_data_tokens(source)
    if not isinstance(source, str):
        return None
    raw = _load_file_payload(source) or source
    d = _raw_json_dict(raw)
    return _cursor_data_tokens(d) if d else _cursor_lines_tokens(raw.splitlines())


def _dict_pct(data):
    pct = data.get('context_pct') or data.get('context_percent')
    return float(pct) if isinstance(pct, (int, float)) and not isinstance(pct, bool) else None


def _cursor_direct_pct(source):
    if isinstance(source, dict):
        return _dict_pct(source)
    raw = _load_file_payload(source) if isinstance(source, str) else ''
    d = _raw_json_dict(raw)
    return _dict_pct(d) if d else None


def cursor_agent_percent(source, cfg, worker=None):
    """Context use of cursor-agent from log output or state file as % of window, or None."""
    pct = _cursor_direct_pct(source)
    if pct is not None:
        return round(pct, 1)
    tokens = cursor_agent_tokens(source)
    return percent(tokens, cfg, worker) if tokens is not None else None


agy_percent = agy_session_percent
cursor_percent = cursor_agent_percent


def seen_percent(line, cfg, worker):
    """The context % one output line states, directly or as a token count against the window; None when it says nothing."""
    m = CONTEXT_PCT.search(line)
    if m:
        return float(m.group(1))
    m = CONTEXT_TOKENS.search(line)
    if m:
        n = float(m.group(1).replace(',', ''))
        n *= {'k': 1e3, 'm': 1e6}.get(m.group(2).lower(), 1)
        return percent(n, cfg, worker)
    return cursor_agent_percent(line, cfg, worker)


class Watch:
    """Follows a CLI run's output or session/state file; `over` turns True once its context reaches the threshold
    (handover workers only)."""

    def __init__(self, cfg, worker, folder=None, session=None, state=None, log=None):
        self.on = enabled(cfg) and mode(worker) == 'handover'
        self.cfg, self.worker = cfg, worker
        self.limit = threshold(cfg, folder) if self.on else None
        self.pct, self.over = None, False
        self.session_path = session
        self.state_path = state
        self.log_path = log

    def update(self, pct):
        if pct is not None and self.on:
            self.pct = pct
            self.over = pct >= self.limit
        return self.over

    def line(self, text):
        if not self.on:
            return False
        pct = seen_percent(text, self.cfg, self.worker)
        return self.update(pct) if pct is not None else self.poll()

    def poll(self):
        return self.check() if (self.session_path or self.state_path or self.log_path) else self.over

    def session(self, path=None):
        target = path or self.session_path
        return self.update(agy_session_percent(target, self.cfg, self.worker)) if target else self.over

    def state(self, source=None):
        target = source or self.state_path or self.log_path
        return self.update(cursor_agent_percent(target, self.cfg, self.worker)) if target else self.over

    def check(self, session=None, state=None):
        self.session(session)
        self.state(state)
        return self.over


def restart_task(task, note):
    """The task of the restarted run: the same task, with the handoff of the run before it."""
    base = task.split('\n\nContinue from this handoff', 1)[0]
    return f'{base}\n\nContinue from this handoff (your context was compacted; the worktree already holds the earlier work):\n\n{note}'


# ---- a local model's conversation, compacted by Tutti


def message_tokens(messages):
    return sum(compact.tokens(m.get('content') or '') for m in messages)


def summary_of(messages):
    """One line per older exchange: what the model ran and the first line of the answer."""
    lines = []
    for m in messages:
        first = (m.get('content') or '').strip().split('\n', 1)[0][:160]
        lines.append(f'- {"you" if m["role"] == "assistant" else "tutti"}: {first}')
    return '\n'.join(lines)


def compact_messages(cfg, worker, messages, folder=None):
    """The conversation, with the older exchanges replaced by a summary once it reaches the threshold of the window;
    unchanged below it. The system prompt, the goal (the first user message) and the newest exchanges stay whole."""
    if not enabled(cfg) or len(messages) <= 2 + 2 * KEEP_EXCHANGES:
        return messages
    if 100.0 * message_tokens(messages) / window(cfg, worker) < threshold(cfg, folder):
        return messages
    head, middle, tail = messages[:2], messages[2:-2 * KEEP_EXCHANGES], messages[-2 * KEEP_EXCHANGES:]
    note = {'role': 'user', 'content': 'Earlier steps, compacted by Tutti (the full output is gone; run a command again if you '
                                       'need it):\n' + summary_of(middle)}
    return [*head, note, {'role': 'assistant', 'content': 'Understood.'}, *tail]


def view(cfg, folder=None):
    """The dashboard's Autocompact setting: the threshold, where it comes from, and how each worker compacts."""
    try:
        chosen = controls.read(folder or config.state_dir(cfg)).get('settings', {}).get('autocompact_pct')
    except OSError:
        chosen = None
    return {'enabled': enabled(cfg), 'threshold_pct': threshold(cfg, folder), 'config_pct': settings(cfg).get('threshold_pct', 60),
            'from': 'dashboard' if valid_pct(chosen) else 'config', 'min': MIN_PCT, 'max': MAX_PCT,
            'workers': {n: mode(w) for n, w in cfg['workers'].items()}}


def tag_jobs(cfg, jobs):
    """Each running job whose context use is known gets facts.context_pct: an agent's transcript (its newest prompt) or
    what a CLI printed about its context."""
    for j in jobs:
        f = j.get('facts') or {}
        worker = cfg['workers'].get(f.get('worker')) if f.get('worker') else None
        tokens = (f.get('tokens') or {}).get('context')
        pct = percent(tokens, cfg, worker)
        if pct is None:
            pct = next((p for p in (seen_percent(x, cfg, worker) for x in reversed(j.get('tail') or [])) if p is not None), None)
        sess = f.get('session') or j.get('session') or f.get('session_file')
        if pct is None and sess:
            pct = agy_session_percent(sess, cfg, worker)
        if pct is not None and j.get('facts') is not None:
            j['facts']['context_pct'] = pct


def errors(cfg):
    a = cfg.get('autocompact')
    if not isinstance(a, dict):
        return [f'autocompact is {a!r}: write it as a mapping, e.g. autocompact: {{threshold_pct: 60}}, or remove the key']
    errs = []
    if not valid_pct(a.get('threshold_pct')):
        errs.append(f'autocompact.threshold_pct is {a.get("threshold_pct")!r}: use a whole percentage from {MIN_PCT} to {MAX_PCT}')
    v = a.get('context_tokens')
    if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v <= 0):
        errs.append(f'autocompact.context_tokens is {v!r}: use the window size in tokens, e.g. 200000')
    for name, w in cfg['workers'].items():
        v = (w or {}).get('context_tokens')
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v <= 0):
            errs.append(f'workers.{name}.context_tokens is {v!r}: use the model\'s window in tokens, e.g. 128000')
    return errs
