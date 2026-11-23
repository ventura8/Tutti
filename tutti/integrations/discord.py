import json
import urllib.request

from .. import secretenv, telemetry


def send(cfg, message, opener=None):
    return _post(cfg, message, opener=opener)


def needs_attention(cfg, items, opener=None):
    message = 'Needs attention:\n' + '\n'.join(f'- {item}' for item in items)
    return _post(cfg, message, opener=opener)


def ask_owner_answer(cfg, question, answer, opener=None):
    message = f'Question: {question}\nAnswer: {answer}'
    return _post(cfg, message, opener=opener)


def _post(cfg, message, opener=None):
    webhook_url = secretenv.get('DISCORD_WEBHOOK_URL')
    if not webhook_url:
        return False
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps({'content': message}).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=5):
            return True
    except Exception as exc:
        telemetry.record('error', 'discord.post', error=str(exc))
        return False
