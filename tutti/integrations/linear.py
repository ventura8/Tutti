import json
import urllib.request

from .. import secretenv, telemetry

API_URL = 'https://api.linear.app/graphql'
CREATE_ISSUE_QUERY = (
    'mutation($title: String!, $description: String!, $teamId: String!) {'
    ' issueCreate(input: {title: $title, description: $description, teamId: $teamId}) { issue { id } }'
    ' }'
)


def create_issue(cfg, title, description, opener=None):
    token = secretenv.get('LINEAR_API_KEY')
    team_id = (cfg or {}).get('integrations', {}).get('linear', {}).get('team_id')
    if not token or not team_id:
        return False
    payload = {'query': CREATE_ISSUE_QUERY, 'variables': {'title': title, 'description': description, 'teamId': team_id}}
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Authorization': token, 'Content-Type': 'application/json'},
        method='POST',
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=5):
            return True
    except Exception as exc:
        telemetry.record('error', 'linear.create_issue', error=str(exc))
        return False
