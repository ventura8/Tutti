import json
import os
import urllib.request


def get_token(cfg):
    # Get token from config
    linear_cfg = cfg.get('linear', {})
    token = linear_cfg.get('token')
    if not token:
        # Fallback to env
        token = os.environ.get('LINEAR_TOKEN')
    return token

class LinearClient:
    def __init__(self, token, workspace):
        self.token = token
        self.workspace = workspace
        self.headers = {'Authorization': token, 'Content-Type': 'application/json'}
        self.api_url = 'https://api.linear.app/graphql'

    def graphql(self, query, variables=None):
        req = urllib.request.Request(
            self.api_url,
            data=json.dumps({'query': query, 'variables': variables}).encode(),
            headers=self.headers,
            method='POST'
        )
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())

    def get_project_issues(self, project_id):
        query = """
        query($projectId: String!) {
            project(id: $projectId) {
                issues {
                    nodes {
                        id
                        title
                        description
                    }
                }
            }
        }
        """
        return self.graphql(query, {'projectId': project_id})

    def create_issue(self, project_id, title, description):
        query = """
        mutation($projectId: String!, $title: String!, $description: String) {
            issueCreate(input: {projectId: $projectId, title: $title, description: $description}) {
                issue {
                    id
                }
            }
        }
        """
        return self.graphql(query, {'projectId': project_id, 'title': title, 'description': description})

    def update_issue(self, issue_id, title, description):
        query = """
        mutation($issueId: String!, $title: String, $description: String) {
            issueUpdate(id: $issueId, input: {title: $title, description: $description}) {
                issue {
                    id
                }
            }
        }
        """
        return self.graphql(query, {'issueId': issue_id, 'title': title, 'description': description})
