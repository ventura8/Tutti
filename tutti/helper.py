"""The reading helper: a bookmarklet the owner adds once from the dashboard (Settings: drag one link to the bookmarks bar).
On a provider's usage page that the owner has open and signed in to (Cursor: cursor.com/dashboard/spending), one click reads
the spend, the limit and the reset date from the page's visible text and opens the dashboard with that reading in the address's
#fragment. The dashboard page then saves it through POST /api/reading with its own token.

What it never does: read cookies, storage or tokens, call the provider's API, or send anything anywhere but this computer's
dashboard. The page text is parsed with patterns, not a stable API: when they find nothing it says so and opens the form.
The fragment carries a per-install key, so another site that opens the dashboard with a made-up reading gets the form, not a save."""
import hmac
import json
import os
import secrets
from urllib.parse import urlsplit

KEY_FILE = 'helper.key'

# The bookmarklet's script, one expression; its comment lines are dropped and its lines joined with spaces.
SCRIPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web', 'helper-bookmarklet.js')


def script():
    with open(SCRIPT_FILE, encoding='utf8') as f:
        return ' '.join(line.strip() for line in f if line.strip() and not line.lstrip().startswith('//'))


def key(state_dir):
    """This install's helper key, created on first use (0600). It only proves a fragment came from the owner's bookmarklet."""
    path = os.path.join(state_dir, KEY_FILE)
    try:
        with open(path, encoding='utf8') as f:
            k = f.read().strip()
        if k:
            return k
    except OSError:
        pass
    k = secrets.token_urlsafe(18)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf8') as f:
        f.write(k + '\n')
    return k


def key_ok(state_dir, given):
    """compare_digest on bytes: on str it raises TypeError for a non-ASCII key, which must be a plain refusal instead."""
    if not isinstance(given, str) or not given:
        return False
    return hmac.compare_digest(given.encode('utf8'), key(state_dir).encode('utf8'))


def helper_accounts(cfg):
    """Accounts a bookmarklet can read: no reader of their own, and a usage page (usage_url)."""
    return {n: a['usage_url'] for n, a in cfg['accounts'].items() if (a or {}).get('usage_url') and not (a or {}).get('reader')}


def helper_pages(cfg):
    """{usage_url: [{account, label}]}: one bookmarklet per page reads every account on it. `helper_label` is the heading
    of the account's meter on the page (Cursor: "Cursor Models", "Other Models"); none: the page's first "N% used"."""
    pages = {}
    for name, url in helper_accounts(cfg).items():
        pages.setdefault(url, []).append({'account': name, 'label': (cfg['accounts'][name] or {}).get('helper_label')})
    return pages


def bookmarklet(targets, k, origin):
    """The javascript: address for one page. `targets` is [{account, label}] (a bare account name for one)."""
    if isinstance(targets, str):
        targets = [{'account': targets, 'label': None}]
    js = script().replace('__TARGETS__', json.dumps(targets)).replace('__KEY__', json.dumps(k))
    # A javascript: address is percent-decoded before it runs: every % of the script is written as %25.
    return 'javascript:' + js.replace('__ORIGIN__', json.dumps(origin)).replace('%', '%25')


def page(cfg, state_dir, port):
    """The helper page (no script: the links are only dragged to the bookmarks bar)."""
    origin = f'http://127.0.0.1:{port}'
    k = key(state_dir)
    items = []
    for url, targets in helper_pages(cfg).items():
        href = bookmarklet(targets, k, origin).replace('&', '&amp;').replace('"', '&quot;')
        host = urlsplit(url).netloc
        names = ', '.join(t['account'] for t in targets)
        link = f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(host)}</a>'
        items.append(f'<li><a class="btn btn--filled" href="{href}">Tutti: read {esc(host)}</a> &nbsp;({esc(names)}) then, on '
                     f'{link}, click it in the bookmarks bar.</li>')
    body = '\n'.join(items) or '<li>No account has a usage_url without its own reader: nothing to read with the helper.</li>'
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light dark">
<title>Tutti reading helper</title><link rel="stylesheet" href="app.css"></head>
<body><main id="main" class="helper"><h1>Reading helper</h1>
<p>Drag a button to your browser's bookmarks bar once. Then open the usage page, signed in as usual, and click the bookmark:
it reads the spend and the reset date from the page you see and opens this dashboard, which saves the reading.</p>
<ul>{body}</ul>
<p>It reads only the visible text of the page you clicked it on. It never reads cookies, passwords or tokens, and sends the
reading only to this dashboard on this computer. If the page changes and nothing is found, it says so and the dashboard
opens its form instead.</p><p><a href="/">Back to the dashboard</a></p></main></body></html>'''.encode()


def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
