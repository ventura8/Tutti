import json
import urllib.request

from .. import secretenv


def send(cfg, message):
    _post(cfg, message)


def needs_attention(cfg, items):
    message = 'Needs attention:\n' + '\n'.join([f'- {item}' for item in items])
    _post(cfg, message)


def ask_owner_answer(cfg, question, answer):
    message = f'Question: {question}\nAnswer: {answer}'
    _post(cfg, message)


def _post(cfg, message):
    webhook_url = secretenv.get('DISCORD_WEBHOOK_URL')
    if not webhook_url:
        return

    data = json.dumps({'content': message}).encode('utf-8')
    headers = {'Content-Type': 'application/json'}

    req = urllib.request.Request(webhook_url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        # Log error
        pass
