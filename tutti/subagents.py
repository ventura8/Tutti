"""`tutti agents count`: how many Claude Code subagents of a session are live, against the owner's limit (`agents.max_claude_subagents`).

Claude Code has no command for it, but it keeps one transcript per subagent at
`~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl`. A subagent is **live** when its transcript has not ended
(its last entry is not a final answer or a delivered report) and was written to within `agents.subagent_active_seconds`
(default 900: long enough for one that waits on background work, short enough that a crashed one stops counting). This is
a heuristic from files, not an API; `--seconds` and the config key tune it. Pure: is_ended, live, verdict; I/O: transcripts."""
import json
import os
import re
import time

DEFAULT_MAX = 3
ACTIVE_SECONDS = 900
TAIL_BYTES = 65536


def project_folder(cwd):
    """Claude Code's folder name for a working directory: every character that is not a letter or digit becomes a dash."""
    return re.sub(r'[^A-Za-z0-9]', '-', os.path.abspath(cwd))


def last_entry(path):
    """The last JSON line of a transcript (read from its tail), or None."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            tail = f.read().decode('utf8', errors='replace')
    except OSError:
        return None
    for line in reversed([x for x in tail.splitlines() if x.strip()]):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        return data if isinstance(data, dict) else None
    return None


def is_ended(entry):
    """Whether a transcript's last entry says the subagent is done: a final answer (an assistant turn that ends without a tool
    call) or the report it handed back."""
    msg = (entry or {}).get('message')
    if not isinstance(msg, dict):
        return False
    if msg.get('role') == 'assistant':
        return msg.get('stop_reason') == 'end_turn'
    text = json.dumps(msg.get('content'))
    return 'Report delivered to your caller' in text


def live(entry, age, active_seconds=ACTIVE_SECONDS):
    """A subagent counts while its transcript has not ended and was written to less than `active_seconds` ago."""
    return entry is not None and not is_ended(entry) and 0 <= age < active_seconds


def session_folder(base, cwd, session=None):
    """The session's folder under `base` (~/.claude/projects): the named session in any project, else the most recently written
    session of this directory's project. None when there is none."""
    if session:
        for proj in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            p = os.path.join(base, proj, session)
            if os.path.isdir(p):
                return p
        return None
    root = os.path.join(base, project_folder(cwd))
    try:
        sessions = [os.path.join(root, n) for n in os.listdir(root) if os.path.isdir(os.path.join(root, n, 'subagents'))
                    or os.path.exists(os.path.join(root, n + '.jsonl'))]
    except OSError:
        return None
    return max(sessions, key=lambda p: os.path.getmtime(p if os.path.isdir(p) else p + '.jsonl'), default=None)


def count(folder, now=None, active_seconds=ACTIVE_SECONDS):
    """[{'id', 'description', 'age'}] of the live subagents in a session folder, oldest first."""
    now = time.time() if now is None else now
    sub = os.path.join(folder, 'subagents')
    out = []
    try:
        names = sorted(n for n in os.listdir(sub) if n.startswith('agent-') and n.endswith('.jsonl'))
    except OSError:
        return out
    for n in names:
        path = os.path.join(sub, n)
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        if live(last_entry(path), age, active_seconds):
            desc = ''
            try:
                with open(path[:-len('.jsonl')] + '.meta.json', encoding='utf8') as f:
                    desc = str(json.load(f).get('description') or '')
            except (OSError, ValueError, AttributeError):
                pass
            out.append({'id': n[len('agent-'):-len('.jsonl')], 'description': desc, 'age': int(age)})
    return out


def verdict(n, limit):
    """('ok' | 'full' | 'over', what to do) for n live subagents against the limit."""
    if n < limit:
        return 'ok', f'{limit - n} more subagent(s) may start'
    if n == limit:
        return 'full', 'at the limit: wait for one to finish before starting another'
    return 'over', f'{n - limit} over the limit: let some finish; start none'
