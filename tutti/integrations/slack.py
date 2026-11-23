import json
import urllib.request

from .. import secretenv, telemetry

API_URL = 'https://slack.com/api/chat.postMessage'


def send(cfg, message, opener=None):
    return _post(cfg, message, opener=opener)


def needs_attention(cfg, items, opener=None):
    message = 'Needs attention:\n' + '\n'.join(f'- {item}' for item in items)
    return _post(cfg, message, opener=opener)


def ask_owner_answer(cfg, question, answer, opener=None):
    message = f'Question: {question}\nAnswer: {answer}'
    return _post(cfg, message, opener=opener)


def _post(cfg, message, opener=None):
    token = secretenv.get('SLACK_TOKEN')
    channel = (cfg or {}).get('integrations', {}).get('slack', {}).get('channel')
    if not token or not channel:
        return False
    req = urllib.request.Request(
        API_URL,
        data=json.dumps({'channel': channel, 'text': message}).encode('utf-8'),
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        method='POST',
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=5):
            return True
    except Exception as exc:
        telemetry.record('error', 'slack.post', error=str(exc))
        return False
