"""Main-agent Chat hand-over (docs/CHAT.md): when the MAIN AGENT ROLE moves (panic or spent), the person keeps talking in
Tutti's dashboard Chat. Tutti writes a Markdown package (HANDOVER.md + per-topic files), loads it into Chat, prepares the
same files for the new agent's desktop app, and Chat's "Main agent now" pick follows whoever holds the role.

Incremental: only new transcript lines since the last cursor. Secrets are redacted. Quiet mode skips heavy transcript I/O.
Never reads credential files."""
import json
import os
import re
import time

from . import backlog, chat, chatctl, config, scrub

CHAT_TITLE = 'Main agent hand-off'   # same title as conduct.CHAT_TITLE
DIR_NAME = 'main-handover'
HANDOVER_FILE = 'HANDOVER.md'
TOPICS_DIR = 'handover'
CURSOR_FILE = 'cursors.json'
META_FILE = 'meta.json'
METRICS_FILE = 'metrics.json'
TRANSCRIPTS_FILE = 'TRANSCRIPTS.md'
BEGIN = '<!-- tutti-handover:begin -->'
END = '<!-- tutti-handover:end -->'
TOOL_NOISE = re.compile(r'^(tool_result|tool_use|function_call|ACTION\s|RUN:\s|\[tool)', re.I)
MAX_TOPIC = 80
MAX_MSG = 4000
MAX_TRANSCRIPT_CHARS = 200_000
MAX_TRANSCRIPTS_MD = 200_000


def folder(cfg):
    return os.path.join(config.state_dir(cfg), DIR_NAME)


def tokens(text):
    """Rough token count (four characters a token), same as chatctl.tokens."""
    return max(1, len(text or '') // 4) if text else 0


def clean(text):
    """User/assistant text with credentials removed; paths and hosts kept short via scrub.text."""
    return scrub.text(scrub.secrets_only(text or ''), quotes=False, limit=MAX_MSG)


def drop_noise(text):
    """Keep decisions and owner words; drop tool-call noise from a turn."""
    lines = [ln for ln in str(text or '').splitlines() if ln.strip() and not TOOL_NOISE.search(ln.strip())]
    return '\n'.join(lines).strip()


def task_line(t):
    return f'{t.get("id", "?")} [{t.get("status", "?")}] {t.get("repo", "?")}: {clean(t.get("title", ""))[:120]}'


def open_tasks(cfg):
    """Running and queued backlog rows as short lines (no secrets)."""
    data = backlog.load(cfg)
    rows = [t for t in data.get('tasks') or [] if t.get('status') in ('queued', 'claimed', 'running', 'needs-person')]
    return [task_line(t) for t in rows[:40]]


def rules_lines(cfg):
    """Owner rules from brief.rules when present, else a short default (never personal paths)."""
    brief = (cfg.get('brief') or {}).get('rules') or []
    if isinstance(brief, str):
        brief = [brief]
    out = [clean(x) for x in brief if str(x).strip()]
    return out[:20] or ['All household work goes through Tutti.', 'Never commit secrets.', 'Ask the owner for push, merge, deploy.']


def build_md(frm, to, why, rules, tasks, topics):
    """Short HANDOVER.md: who took over, rules, open tasks, topic index. Pure."""
    lines = [
        '# Hand-over for the next main agent',
        '',
        f'MAIN AGENT ROLE: **{to or frm}**' + (f' (from {frm})' if to and to != frm else ''),
        f'Reason: {clean(why) or "panic or spent"}',
        '',
        '## Owner rules (short)',
        *[f'- {r}' for r in rules],
        '',
        '## Open tasks',
        *([f'- {t}' for t in tasks] or ['- (none)']),
        '',
        '## Topics (open on demand)',
        *([f'- `{name}.md`' for name in topics] or ['- (none)']),
        '',
        'Full transcripts stay as browsable Chat threads; read them only when needed.',
    ]
    return '\n'.join(lines) + '\n'


def topic_name(title):
    raw = re.sub(r'[^a-z0-9]+', '-', str(title or 'topic').lower()).strip('-')[:MAX_TOPIC]
    return raw or 'topic'


def topics_from_tasks(tasks):
    """One short topic file body per repo mentioned in open task lines."""
    out = {}
    for line in tasks:
        m = re.search(r'\]\s+([^:]+):', line)
        repo = topic_name(m.group(1) if m else 'work')
        out.setdefault(repo, []).append(f'- {line}')
    return {name: f'# {name}\n\n' + '\n'.join(body) + '\n' for name, body in out.items()}


def parse_jsonl_turn(obj):
    """One Claude-style JSONL record → (role, text) or None. User/assistant text only."""
    if not isinstance(obj, dict):
        return None
    role = obj.get('type') or obj.get('role')
    if role not in ('user', 'assistant', 'human'):
        return None
    role = 'user' if role in ('user', 'human') else 'assistant'
    msg = obj.get('message') if isinstance(obj.get('message'), dict) else obj
    content = msg.get('content') if isinstance(msg, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [p.get('text', '') for p in content if isinstance(p, dict) and p.get('type', 'text') == 'text']
        text = '\n'.join(parts)
    else:
        text = str(msg.get('text') or obj.get('text') or '')
    text = drop_noise(clean(text))
    return (role, text) if text else None


def read_new_turns(path, offset=0, limit=MAX_TRANSCRIPT_CHARS):
    """Incremental: turns after byte `offset`. Returns (turns, new_offset)."""
    turns, size = [], offset
    try:
        size = os.path.getsize(path)
        start = max(0, int(offset or 0))
        with open(path, 'rb') as f:
            f.seek(start)
            chunk = f.read(limit)
        if not chunk:
            return [], start
        cut = len(chunk)
        if start + cut < size:
            last_nl = chunk.rfind(b'\n')
            if last_nl < 0:
                return [], start
            cut = last_nl + 1
        new_offset = start + cut
        for line in chunk[:cut].decode('utf8', errors='replace').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                turn = parse_jsonl_turn(json.loads(line))
            except ValueError:
                continue
            if turn:
                turns.append(turn)
        return turns, new_offset
    except OSError:
        return [], offset


def write_atomic(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_json(path, data):
    write_atomic(path, json.dumps(data, indent=2) + '\n')


def load_json(path, default=None):
    try:
        with open(path, encoding='utf8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default if default is not None else {}


def write_package(cfg, frm, to, why, started=None):
    """Build and write HANDOVER.md + topic files + metrics. Returns the package dict."""
    started = started if started is not None else time.time()
    root = folder(cfg)
    os.makedirs(os.path.join(root, TOPICS_DIR), mode=0o700, exist_ok=True)
    tasks, rules = open_tasks(cfg), rules_lines(cfg)
    topics = topics_from_tasks(tasks)
    md = build_md(frm, to, why, rules, tasks, list(topics))
    write_atomic(os.path.join(root, HANDOVER_FILE), md)
    for name, body in topics.items():
        write_atomic(os.path.join(root, TOPICS_DIR, f'{name}.md'), body)
    metrics = {
        'md_bytes': len(md.encode()), 'md_tokens': tokens(md),
        'topic_bytes': sum(len(b.encode()) for b in topics.values()),
        'seconds': round(time.time() - started, 3),
        'from': frm, 'to': to, 'at': time.time(),
    }
    write_json(os.path.join(root, METRICS_FILE), metrics)
    write_json(os.path.join(root, META_FILE), {'from': frm, 'to': to, 'why': why, 'at': time.time()})
    return {'md': md, 'topics': topics, 'metrics': metrics, 'root': root}


def transcript_md_path(cfg):
    return os.path.join(folder(cfg), TRANSCRIPTS_FILE)


def owner_sub(cfg, sub=None):
    """Chat partition for the hand-over thread: explicit `sub`, else the signed-in owner's (conduct.owner_chat_sub)."""
    if sub is not None:
        return sub
    from . import conduct
    return conduct.owner_chat_sub(cfg)


def seed_chat(cfg, package, note, sub=None):
    """Load HANDOVER.md into the owner's hand-off conversation; set Chat default model to `main`."""
    sub = owner_sub(cfg, sub)
    store = chat.Store(config.state_dir(cfg))
    conv = next((c for c in store.load(sub)['conversations'] if c.get('title') == CHAT_TITLE), None)
    cid = (conv or store.create(sub, title=CHAT_TITLE))['id']

    def put(data):
        c = store.conversation(data, cid)
        chatctl.apply(data, c, {'model': 'main'})
        return c

    store.change(sub, put)
    body = f'{note}\n\n---\n\n{package["md"]}'
    store.put_message(sub, cid, {
        'id': chat.new_id('m'), 'role': 'assistant', 'text': body, 'at': chat.now_iso(),
        'status': 'done', 'model': 'tutti', 'cards': [],
    })
    return cid


def import_turns_as_thread(cfg, title, turns, sub='local'):
    """Append redacted turns as a browsable Chat thread (user/assistant messages)."""
    if not turns:
        return None
    store = chat.Store(config.state_dir(cfg))
    conv = next((c for c in store.load(sub)['conversations'] if c.get('title') == title), None)
    cid = (conv or store.create(sub, title=title))['id']
    for role, text in turns:
        store.put_message(sub, cid, {
            'id': chat.new_id('m'), 'role': role, 'text': text, 'at': chat.now_iso(),
            'status': 'done', 'model': 'handover' if role == 'assistant' else None, 'cards': [],
        })
    return cid


def sync_transcript(cfg, path, title, sub='local'):
    """Incremental import of one JSONL transcript into Chat. Returns metrics for this file."""
    cursors = load_json(os.path.join(folder(cfg), CURSOR_FILE), {})
    offset = int(cursors.get(path) or 0)
    turns, new_off = read_new_turns(path, offset)
    if turns:
        import_turns_as_thread(cfg, title, turns, sub=sub)
        append_transcript_md(cfg, title, turns)
        cursors[path] = new_off
        write_json(os.path.join(folder(cfg), CURSOR_FILE), cursors)
    return {'path': path, 'turns': len(turns), 'bytes': max(0, new_off - offset)}


def _turn_block(role, text):
    body = str(text or '').strip()
    if '\n' in body:
        return f'**{role}**\n\n```\n{body}\n```\n'
    return f'**{role}** {body}\n'


def _trim_transcript_md(text):
    if len(text) <= MAX_TRANSCRIPTS_MD:
        return text
    return ('# Imported transcripts (latest)\n\nOlder transcript content was trimmed to '
            'keep this file small.\n\n' + text[-MAX_TRANSCRIPTS_MD:])



def append_transcript_md(cfg, title, turns):
    if not turns:
        return
    path = transcript_md_path(cfg)
    try:
        with open(path, encoding='utf8') as f:
            prev = f.read()
    except OSError:
        prev = '# Imported transcripts (latest)\n\n'
    stamp = time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())
    lines = [f'## {title} ({stamp})', '']
    lines += [_turn_block(role, text) for role, text in turns]
    write_atomic(path, _trim_transcript_md(prev + '\n'.join(lines) + '\n'))


def transcript_roots(cfg, home=None):
    """Configured transcript dirs (main.transcripts), else Claude's default under `home` when it exists."""
    home = home or os.path.expanduser('~')
    raw = (cfg.get('main') or {}).get('transcripts') or {}
    if isinstance(raw, list):
        return [config.expand(p) for p in raw]
    if isinstance(raw, dict) and raw:
        return [config.expand(p) for p in raw.values()]
    claude = os.path.join(home, '.claude', 'projects')
    return [claude] if os.path.isdir(claude) else []


def list_jsonl(root, limit=20):
    out = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith('.jsonl'):
                out.append(os.path.join(dirpath, name))
            if len(out) >= limit:
                return out
    return out


def sync_all_transcripts(cfg, home=None, sub=None, quiet=False):
    """Import new turns from each transcript root. Skips when quiet mode asks for low I/O."""
    if quiet:
        return {'skipped': 'quiet', 'files': 0, 'turns': 0}
    sub = owner_sub(cfg, sub)
    total_turns, files = 0, 0
    for root in transcript_roots(cfg, home=home):
        for path in list_jsonl(root):
            title = f'Hand-over · {os.path.basename(path)[:60]}'
            m = sync_transcript(cfg, path, title, sub=sub)
            files += 1
            total_turns += m['turns']
    return {'files': files, 'turns': total_turns}


def desktop_targets(kind, home):
    """Paths under `home` where the new agent's app reads context (Cursor rules, GEMINI/AGENTS)."""
    home = home or os.path.expanduser('~')
    if kind in ('cursor',):
        return [os.path.join(home, '.cursor', 'rules', 'tutti-handover.md')]
    if kind in ('agy', 'antigravity', 'gemini'):
        return [os.path.join(home, '.gemini', 'GEMINI.md')]
    if kind in ('claude', 'claude-code'):
        return [os.path.join(home, '.claude', 'CLAUDE.md')]
    if kind in ('codex',):
        return [os.path.join(home, '.codex', 'AGENTS.md')]
    return [os.path.join(home, 'HANDOVER.md')]


def splice_block(existing, body):
    """Replace or append the marked hand-over block in an instructions file."""
    block = f'{BEGIN}\n{body.rstrip()}\n{END}\n'
    if BEGIN in existing and END in existing:
        pre, rest = existing.split(BEGIN, 1)
        _old, post = rest.split(END, 1)
        return pre.rstrip() + '\n\n' + block + post.lstrip()
    return (existing.rstrip() + '\n\n' if existing.strip() else '') + block


def write_desktop(cfg, to, package, home=None):
    """Write HANDOVER.md for the new agent's desktop app (Cursor rules / GEMINI / CLAUDE / AGENTS)."""
    from . import users
    kind = users.kind_of(cfg, to) or to
    transcript_file = transcript_md_path(cfg)
    transcript_name = 'tutti-handover-transcripts.md'
    has_transcript = os.path.isfile(transcript_file)
    body = package['md'] + '\nRead topic files under Tutti state `main-handover/handover/` when a topic comes up.\n'
    if has_transcript:
        body += f'Read imported chats in `{transcript_name}` for full conversation context.\n'
    written = []
    for path in desktop_targets(kind, home):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        try:
            with open(path, encoding='utf8') as f:
                prev = f.read()
        except OSError:
            prev = ''
        write_atomic(path, splice_block(prev, body), mode=0o600)
        written.append(path)
        if has_transcript:
            with open(transcript_file, encoding='utf8') as f:
                write_atomic(os.path.join(os.path.dirname(path), transcript_name), f.read(), mode=0o600)
            written.append(os.path.join(os.path.dirname(path), transcript_name))
    state_copy = os.path.join(package['root'], f'for-{to}.md')
    write_atomic(state_copy, body)
    written.append(state_copy)
    return written


def is_quiet(cfg):
    try:
        from . import quietmode
        return bool(quietmode.status(cfg).get('quiet'))
    except Exception:
        return False


def deliver(cfg, frm, to, why, home=None, sub=None, seed=True, note=None):
    """Full hand-over: package → Chat (unless seed is False) → transcripts (unless quiet) → desktop. Returns the note."""
    started = time.time()
    package = write_package(cfg, frm, to, why, started=started)
    note = note or (
        (f'MAIN AGENT ROLE handed from {frm} to {to}: {why}' if to else
         f'MAIN AGENT ROLE stays with {frm} (out: {why}); no agent in main.fallback has room')
        + f'. Hand-over package: {package["metrics"]["md_tokens"]} tokens of Markdown.')
    sub = owner_sub(cfg, sub)
    if seed:
        seed_chat(cfg, package, note, sub=sub)
    sync = sync_all_transcripts(cfg, home=home, sub=sub, quiet=is_quiet(cfg))
    if to:
        write_desktop(cfg, to, package, home=home)
    package['metrics']['transcript'] = sync
    package['metrics']['seconds'] = round(time.time() - started, 3)
    write_json(os.path.join(package['root'], METRICS_FILE), package['metrics'])
    return note


def md_hash(cfg):
    try:
        with open(os.path.join(folder(cfg), HANDOVER_FILE), encoding='utf8') as f:
            return hash(f.read())
    except OSError:
        return None


def refresh(cfg, frm, to, why, home=None, sub=None):
    """Rebuild HANDOVER.md while panic lasts; re-seed Chat only when the Markdown changed; sync new transcript lines."""
    sub = owner_sub(cfg, sub)
    before = md_hash(cfg)
    package = write_package(cfg, frm, to, why)
    changed = md_hash(cfg) != before
    note = None
    if changed:
        note = f'Hand-over refreshed for {to or frm} (still: {clean(why)}). {package["metrics"]["md_tokens"]} MD tokens.'
        seed_chat(cfg, package, note, sub=sub)
    sync = sync_all_transcripts(cfg, home=home, sub=sub, quiet=is_quiet(cfg))
    if to and (changed or sync.get('turns')):
        write_desktop(cfg, to, package, home=home)
    return note


def current_agent(cfg):
    """The main agent now, or '' when the config has no `main` block (Chat fixtures without conduct)."""
    from . import conduct
    if not isinstance(cfg.get('main'), dict):
        return ''
    try:
        return conduct.current(cfg)
    except (KeyError, TypeError, conduct.ConductError):
        return str((cfg.get('main') or {}).get('agent') or '')


KIND_SHOW = {
    'claude': 'Claude', 'cursor': 'Cursor', 'agy': 'Antigravity', 'gemini': 'Gemini',
    'codex': 'Codex', 'copilot': 'Copilot', 'local': 'local',
}


def status_for_chat(cfg):
    """What the Chat page shows: current main agent name (with kind) and last hand-over metrics."""
    from . import users
    agent = current_agent(cfg) or next(iter(cfg.get('workers') or {}), '') or '?'
    state = (cfg.get('project') or {}).get('state')
    meta = load_json(os.path.join(folder(cfg), META_FILE), {}) if state else {}
    metrics = load_json(os.path.join(folder(cfg), METRICS_FILE), {}) if state else {}
    w = (cfg.get('workers') or {}).get(agent) or {}
    kind = w.get('kind') or 'cli'
    show = KIND_SHOW.get(users.kind_of(cfg, agent) or '', 'local' if kind == 'ollama' else 'CLI')
    label = f'{agent} ({show})'
    return {'name': agent, 'label': label, 'kind': kind, 'show': show,
            'handover': {'from': meta.get('from'), 'to': meta.get('to'), 'why': meta.get('why'),
                         'md_tokens': metrics.get('md_tokens'), 'seconds': metrics.get('seconds'),
                         'transcript': metrics.get('transcript')}}
