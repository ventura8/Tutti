"""Home Assistant REST API client (stdlib only, token from Tutti secrets)."""
import json
import os
import urllib.error
import urllib.request

DEFAULT_TOKEN_PATH = '~/.config/tutti/secrets/home-assistant.token'


def _token_path(ha_cfg=None):
    path = (ha_cfg or {}).get('token_file') or DEFAULT_TOKEN_PATH
    return os.path.expanduser(path)


def get_token(ha_cfg=None):
    try:
        with open(_token_path(ha_cfg), encoding='utf8') as f:
            return f.read().strip() or None
    except OSError:
        return None


class HAClient:
    """Thin REST client for Home Assistant media_player and remote entities."""

    def __init__(self, cfg, opener=None, token=None):
        ha_cfg = (cfg or {}).get('home_assistant') or {}
        self.url = str(ha_cfg.get('url') or '').rstrip('/')
        self.token = token if token is not None else get_token(ha_cfg)
        self._opener = opener or urllib.request.urlopen

    def configured(self):
        return bool(self.url and self.token)

    @property
    def headers(self):
        return {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
        }

    def _request(self, method, path, payload=None):
        if not self.configured():
            return None
        data = json.dumps(payload).encode('utf8') if payload is not None else None
        req = urllib.request.Request(f'{self.url}{path}', data=data, headers=self.headers, method=method)
        try:
            with self._opener(req, timeout=10) as resp:
                return json.loads(resp.read().decode('utf8') or '{}')
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def get_state(self, entity_id):
        return self._request('GET', f'/api/states/{entity_id}')

    def list_states(self):
        res = self._request('GET', '/api/states')
        return res if isinstance(res, list) else []

    def call_service(self, domain, service, service_data):
        return self._request('POST', f'/api/services/{domain}/{service}', service_data)
