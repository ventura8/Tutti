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
    token = secretenv.get('SLACK_TOKEN')
    channel = cfg.get('integrations', {}).get('slack', {}).get('channel')
    if not token or not channel:
        return

    url = 'https://slack.com/api/chat.postMessage'
    data = json.dumps({'channel': channel, 'text': message}).encode('utf-8')
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }

    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        # Log error
        pass
