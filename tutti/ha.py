import os

import requests


def _token_path():
    return os.path.expanduser('~/.config/tutti/secrets/home-assistant.token')

def get_token():
    try:
        with open(_token_path(), encoding='utf8') as f:
            return f.read().strip()
    except OSError:
        return None

class HAClient:
    def __init__(self, cfg):
        ha_cfg = cfg.get('home_assistant', {})
        self.url = ha_cfg.get('url', '').rstrip('/')
        self.token = get_token()

    @property
    def headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def get_state(self, entity_id):
        if not self.url or not self.token:
            return None
        response = requests.get(f"{self.url}/api/states/{entity_id}", headers=self.headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def call_service(self, domain, service, service_data):
        if not self.url or not self.token:
            return None
        url = f"{self.url}/api/services/{domain}/{service}"
        response = requests.post(url, headers=self.headers, json=service_data)
        response.raise_for_status()
        return response.json()
