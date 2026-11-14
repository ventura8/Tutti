"""What telemetry may keep: scrubbing of free text and allowlisting of values. Pure functions, no I/O.

Two rules. A field value is a *category* (a command name, a worker kind, an outcome: letters, digits and `_.:+-`, at most
64 characters) or a number; anything else becomes `<other>`, so task text, prompts and code can never pass as a value.
Free text (an exception's message, a dashboard script error) goes through `text()`: home folders become `~`, and e-mail
addresses, URLs, IP addresses, host names, user@host, tokens, keys and quoted fragments are replaced by a placeholder."""
import os
import re

TOKEN = '<token>'
CATEGORY_RX = re.compile(r'[A-Za-z0-9_.:+-]{1,64}')
OTHER = '<other>'
# A name followed by these is a host or domain name: `api.example.com`, `box.lan`, `pc.local`. A file name such as
# `tutti.config.example.yaml` is left alone (the name goes on after the suffix).
TLDS = ('com|net|org|io|dev|ai|app|co|uk|de|fr|ro|eu|us|info|biz|me|cloud|local|lan|home|internal|corp|intranet|example|'
        'localdomain|test|invalid|onion')
RULES = (
    # secrets first, before their parts look like something else
    (re.compile(r'-----BEGIN [A-Z ]+-----[\s\S]*?-----END [A-Z ]+-----'), '<key>'),
    (re.compile(r'-----BEGIN [A-Z ]+-----[\s\S]*'), '<key>'),   # a key block cut off before its end line
    (re.compile(r'(?i)\b(bearer|basic|token)\s+[A-Z0-9._~+/=-]{8,}'), r'\1 ' + TOKEN),
    (re.compile(r'(?i)\b([\w-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|auth|dsn|cookie|'
                r'session)[\w-]*)(\s*[=:]\s*)("[^"]*"|\'[^\']*\'|\S+)'), r'\1\2<redacted>'),
    (re.compile(r'\b(sk|pk|rk)[-_](live|test|ant|proj)?[-_]?[A-Za-z0-9_-]{12,}'), TOKEN),
    (re.compile(r'\b(ghp|gho|ghu|ghs|ghr|github_pat|glpat|xox[abprs]|AKIA|ASIA|AIza|ya29|hf)[_.-]?[A-Za-z0-9_-]{10,}'), TOKEN),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'), TOKEN),   # a JWT
    (re.compile(r'[a-z][a-z0-9+.-]*://\S+', re.I), '<url>'),
    (re.compile(r'[\w.+-]+@[\w-]+(\.[\w-]+)*'), '<email>'),        # e-mail addresses and user@host alike
    (re.compile(r'\b\d{1,3}(\.\d{1,3}){3}(:\d+)?\b'), '<ip>'),
    (re.compile(r'\b[0-9a-f]{0,4}(:[0-9a-f]{0,4}){2,7}:[0-9a-f]{1,4}\b', re.I), '<ip>'),
    (re.compile(r'\b([a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(' + TLDS + r')\b(?!\.?[\w-])', re.I), '<host>'),
    # long runs that look like keys or ids: hex of 24 or more, or mixed letters and digits of 32 or more
    (re.compile(r'\b[0-9a-f]{24,}\b', re.I), TOKEN),
    (re.compile(r'\b(?=[A-Za-z0-9_+/=-]*\d)(?=[A-Za-z0-9_+/=-]*[A-Za-z])[A-Za-z0-9_+/=-]{32,}'), TOKEN),
)
KEY_RXS = (RULES[4][0], RULES[5][0])   # the sk-... and ghp_... shapes: a category value never looks like a key
QUOTED = re.compile(r'"[^"\n]*"|\'[^\'\n]*\'|`[^`\n]*`|\u201c[^\u201d\n]*\u201d')
HOME_RX = re.compile(r'(/home|/Users|/var/home|/data/data/com\.termux/files/home)/[^/\s:\'"`]+|[A-Za-z]:\\Users\\[^\\\s:\'"`]+')


def category(value):
    """A value fit to keep: a category string, a number, a bool or None; anything else is `<other>`."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return round(value, 3)
    s = str(value)
    return s if CATEGORY_RX.fullmatch(s) and not any(rx.search(s) for rx in KEY_RXS) else OTHER


def fields(data, limit=24):
    """The allowlisted shape of an event's fields: names are categories, values are categories or short lists of them."""
    out = {}
    for k, v in list((data or {}).items())[:limit]:
        name = category(k)
        if not isinstance(name, str) or name == OTHER:
            continue
        out[name] = [category(x) for x in list(v)[:20]] if isinstance(v, (list, tuple, set)) else category(v)
    return out


def homes(home=None):
    """This user's home folder, in every spelling a message may carry it."""
    home = home if home is not None else os.path.expanduser('~')
    return [h for h in {home, os.path.realpath(home) if home else ''} if h and h != '/']


def text(value, home=None, hosts=(), limit=400, quotes=True):
    """Free text with everything personal replaced. `hosts`: names known to be personal (this computer's, the machines')."""
    s = str(value or '')
    for h in sorted(homes(home), key=len, reverse=True):
        s = s.replace(h, '~')
    s = HOME_RX.sub('~', s)
    for h in sorted({x for x in hosts if x and len(x) > 1}, key=len, reverse=True):
        s = re.sub(r'(?<![\w-])' + re.escape(h) + r'(?![\w-])', '<host>', s, flags=re.I)
    if quotes:   # what a message quotes is usually what the user typed: a title, a task, a path, a value
        s = QUOTED.sub('<...>', s)
    for rx, repl in RULES:
        s = rx.sub(repl, s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:limit] + ('...' if len(s) > limit else '')


def path(filename, package_root, home=None):
    """A stack frame's file: relative to the package for Tutti's own files, else only its last two parts (no folders of
    the user's), home folders replaced."""
    if not filename:
        return '?'
    full = os.path.abspath(filename)
    root = os.path.abspath(package_root)
    if full.startswith(root + os.sep):
        return os.path.relpath(full, os.path.dirname(root)).replace(os.sep, '/')
    parts = full.replace('\\', '/').split('/')
    return text('/'.join(parts[-2:]), home=home, quotes=False, limit=120)


def url_path(value):
    """A page URL reduced to its path on the dashboard (`/app.js`): never a host, a query or a fragment."""
    m = re.match(r'^[a-z][a-z0-9+.-]*://[^/]*(/[^?#\s]*)?', str(value or ''), re.I)
    p = (m.group(1) or '/') if m else str(value or '').split('?', 1)[0].split('#', 1)[0]
    return p if re.fullmatch(r'/[A-Za-z0-9_./-]{0,80}', p) else OTHER
