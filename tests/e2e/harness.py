"""The browser tests' world (tests/e2e/test_ui.py): the dashboard of serve.py in a child process, and sign-in through the
dashboard's own login route with ID tokens a fake Google signs (tests/test_auth.py's key and claims). Never a real account."""
import json
import os
import re
import subprocess
import sys
import tempfile

from test_auth import claims, token

from tutti import auth

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


class Dashboard:
    """serve.py running in a temp home; `base` is its http://127.0.0.1:<port> address."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tutti-e2e-')
        env = {**os.environ, 'TUTTI_NO_DAEMONS': '1', 'HOME': self.tmp.name, 'XDG_CONFIG_HOME': os.path.join(self.tmp.name, '.config'),
               'TUTTI_TELEMETRY': 'off', 'PYTHONPATH': os.pathsep.join([ROOT, os.path.join(ROOT, 'tests')]), 'TZ': 'UTC'}
        self.proc = subprocess.Popen([sys.executable, os.path.join(HERE, 'serve.py'), self.tmp.name], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, env=env, cwd=self.tmp.name)
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f'serve.py exited with {self.proc.wait()} before serving: run it by hand to see why')
        info = json.loads(line)
        self.port, self.client = info['port'], info['client']
        self.key = tuple(int(k) for k in info['key'])
        self.base = f'http://127.0.0.1:{self.port}'

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.tmp.cleanup()

    def sign_in(self, context, sub):
        """Signs `sub` in within `context` the way Google's button does: the login page's cookie, its nonce in the ID token.
        Returns the answer ({ok, role})."""
        page = context.new_page()
        page.goto(f'{self.base}/login')
        secret = next(c['value'] for c in context.cookies() if c['name'] == f'tutti_login_{self.port}')
        tok = token(self.key, claims(sub=sub, email=f'{sub}@example.com', aud=self.client, nonce=auth.nonce_of(secret), name=sub))
        r = page.request.post(f'{self.base}/api/auth/login', data=json.dumps({'credential': tok}),
                              headers={'Content-Type': 'application/json', 'Origin': self.base})
        page.close()
        if r.status != 200:
            raise RuntimeError(f'the fake Google sign-in of {sub} was refused: {r.status} {r.text()}')
        return r.json()

    def admin_post(self, context, path, body):
        """A POST from the admin's page, with its CSRF token (the page's tutti-token meta)."""
        page = context.new_page()
        html = page.request.get(f'{self.base}/').text()
        csrf = re.search(r'name="tutti-token" content="([^"]*)"', html).group(1)
        r = page.request.post(f'{self.base}{path}', data=json.dumps(body),
                              headers={'Content-Type': 'application/json', 'Origin': self.base, 'X-Tutti-Token': csrf})
        page.close()
        if r.status != 200:
            raise RuntimeError(f'POST {path} was refused: {r.status} {r.text()}')
        return r.json()
