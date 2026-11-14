"""Tutti's own, built-in Google sign-in clients and the release defaults (docs/AUTH.md, *Tutti's own sign-in*).

A new household needs no Google Cloud console: like gh and gcloud, a release of Tutti carries its own public OAuth
clients, from the maintainer's Google project:

- the web client (the "Sign in with Google" button on http://localhost:4777 and http://127.0.0.1:4777): its ID only;
- the "TVs and Limited Input devices" client (the code and QR from any LAN or VPN address): its ID, and its installed-app
  secret, which Google documents as not confidential. The secret is never in the repository: the release build writes it
  into `tutti/release.json` from the GitHub Actions secret TUTTI_GOOGLE_DEVICE_SECRET (scripts/release-assets.sh).

The built-in clients are used only by a release build (one whose tarball carries `release.json`), and only when the
household set no client of its own: a client saved by `tutti auth setup` or `auth.client_id` in the config always wins.
`auth.official: false` in the config turns them off. A release built without the secret has the button client only;
the code from a LAN address then needs the household's own device client (`tutti auth setup`).

`release.json` may also carry the maintainer's default Sentry DSN (DSNs are public). It is used only when sending is on
(`tutti telemetry on`) and no DSN of the household's own is set."""
import json
import os

WEB_CLIENT_ID = '325712322612-drojn5kgsa1n7u0r3oc81ht5731nig49.apps.googleusercontent.com'
DEVICE_CLIENT_ID = '325712322612-vuo0ur4lnn4njpu5740spc9vuk4voh9v.apps.googleusercontent.com'
WEB_PORT = 4777       # the web client authorizes http://localhost:4777 and http://127.0.0.1:4777 only
RELEASE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'release.json')


def release(path=None):
    """What the release build wrote ({} in a checkout or a build without the file)."""
    try:
        with open(path or RELEASE_FILE, encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def on(cfg, path=None):
    """Whether the built-in clients may be used: a release build, and `auth.official` not set to false."""
    if ((cfg or {}).get('auth') or {}).get('official') is False:
        return False
    return release(path).get('official') is True


def web_client(cfg, path=None):
    """The built-in button client's ID, or '' (not a release build, turned off, or the dashboard is not on its port)."""
    port = ((cfg or {}).get('dashboard') or {}).get('port') or WEB_PORT
    return WEB_CLIENT_ID if on(cfg, path) and str(port).strip() == str(WEB_PORT) else ''


def device_client(cfg, path=None):
    """(client ID, secret) of the built-in device client, or ('', '') when this build has no secret."""
    secret = (release(path).get('device_secret') or '').strip() if on(cfg, path) else ''
    return (DEVICE_CLIENT_ID, secret) if secret else ('', '')


def sentry_dsn(path=None):
    """The release's default Sentry DSN, or ''."""
    return (release(path).get('sentry_dsn') or '').strip()
