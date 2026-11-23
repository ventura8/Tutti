from . import discord, linear, slack

CHAT_PROVIDERS = (('slack', slack), ('discord', discord))


def _enabled(cfg, name):
    return bool((cfg or {}).get('integrations', {}).get(name, {}).get('enabled'))


def _call_chat(cfg, method, *args):
    for name, mod in CHAT_PROVIDERS:
        if _enabled(cfg, name):
            getattr(mod, method)(cfg, *args)


def notify(cfg, message):
    _call_chat(cfg, 'send', message)


def task_created(cfg, task):
    notify(cfg, f'New task {task["id"]}: {task["title"]}')
    if _enabled(cfg, 'linear'):
        desc = f'Task {task["id"]} ({task.get("repo", "")}) added to backlog'
        linear.create_issue(cfg, task['title'], desc)


def needs_attention(cfg, items):
    if items:
        _call_chat(cfg, 'needs_attention', items)


def ask_owner_answer(cfg, question, answer):
    _call_chat(cfg, 'ask_owner_answer', question, answer)


def _handle_ask_owner(cfg, rest):
    parts = rest.split(None, 1)
    if len(parts) < 2:
        return 'Usage: !ask_owner <question-id> <answer>'
    from .. import needs
    _ok, msg = needs.answer(cfg, parts[0], parts[1])
    return msg


def handle_command(cfg, message):
    text = (message or '').strip()
    if text == '!backlog list':
        from .. import backlog
        return backlog.listing(backlog.load(cfg))
    if text == '!needs':
        from .. import needs
        return needs.lines_for_agent(cfg, 'admin', 'owner')
    if text == '!ask_owner' or text.startswith('!ask_owner '):
        return _handle_ask_owner(cfg, text[len('!ask_owner'):].strip())
    return 'Unknown command'
