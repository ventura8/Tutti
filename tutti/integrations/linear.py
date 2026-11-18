import json
import urllib.request

from .. import secretenv


def send(cfg, message):
    # Linear doesn't have a simple notification API,
    # but we can create an issue for it.
    create_issue(cfg, 'Notification', message)


def create_issue(cfg, title, description):
    token = secretenv.get('LINEAR_API_KEY')
    team_id = cfg.get('integrations', {}).get('linear', {}).get('team_id')
    if not token or not team_id:
        return

    url = 'https://api.linear.app/graphql'
    query = """
    mutation($title: String!, $description: String!, $teamId: String!) {
        issueCreate(input: {title: $title, description: $description, teamId: $teamId}) {
            issue {
                id
            }
        }
    }
    """
    variables = {'title': title, 'description': description, 'teamId': team_id}
    data = json.dumps({'query': query, 'variables': variables}).encode('utf-8')
    headers = {
        'Authorization': token,
        'Content-Type': 'application/json'
    }

    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        # Log error
        pass
