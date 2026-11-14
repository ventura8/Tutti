"""Cursor's usage, read with no owner click: the same account call the spending page makes (DashboardService
GetCurrentPeriodUsage on api2.cursor.sh), signed in with the token cursor-agent's own login keeps in ~/.config/cursor/auth.json.
It spends no quota and answers both meters of cursor.com/dashboard/spending: `autoPercentUsed` ("Cursor Models": Auto,
Composer, Grok) and `apiPercentUsed` ("Other Models"), with the billing cycle's end as the reset. Grok Bot's weekly allowance
is not in this answer: it keeps the reading helper.

The token is read here, sent only to api2.cursor.sh over HTTPS, and never logged, printed or kept; an error names what to do
(`cursor-agent login`), never the token. An account opts in with `reader: {format: cursor-api, field: autoPercentUsed}`."""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

URL = 'https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage'
LOGIN_FILE = '~/.config/cursor/auth.json'   # where cursor-agent keeps its login
FIELDS = ('autoPercentUsed', 'apiPercentUsed', 'totalPercentUsed')
TIMEOUT = 20


def token(path=LOGIN_FILE):
    """The access token of cursor-agent's login, or (None, why)."""
    try:
        with open(os.path.expanduser(path), encoding='utf8') as f:
            t = (json.load(f) or {}).get('accessToken')
    except (OSError, ValueError, AttributeError):
        t = None
    return (t, None) if isinstance(t, str) and t else (None, f'cursor-agent is not signed in on this computer ({path} has no '
                                                              'login): run `cursor-agent login` once')


def call(tok, opener=urllib.request.urlopen):
    req = urllib.request.Request(URL, data=b'{}', method='POST',
                                 headers={'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'})
    with opener(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode('utf8'))


def fetch(path=LOGIN_FILE, opener=urllib.request.urlopen):
    """(answer, problem): the trimmed answer {planUsage: {autoPercentUsed, apiPercentUsed, totalPercentUsed}, billingCycleEnd}."""
    tok, why = token(path)
    if not tok:
        return None, why
    try:
        d = call(tok, opener)
    except urllib.error.HTTPError as e:
        hint = ': run `cursor-agent login` again (the login expired)' if e.code in (401, 403) else ': it is asked again later'
        return None, f'Cursor\'s usage call answered {e.code}{hint}'
    except (OSError, ValueError) as e:
        return None, f'Cursor\'s usage call failed ({type(e).__name__}): check the network; it is asked again later'
    plan = (d or {}).get('planUsage') if isinstance(d, dict) else None
    if not isinstance(plan, dict):
        return None, 'Cursor\'s usage answer has no planUsage: its format changed; use the reading helper until Tutti is updated'
    return {'planUsage': {k: plan.get(k) for k in FIELDS}, 'billingCycleEnd': d.get('billingCycleEnd')}, None


def parse(data, field):
    """(used %, reset datetime or None) from fetch's answer."""
    v = ((data or {}).get('planUsage') or {}).get(field)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f'no "{field}" in Cursor\'s usage answer (it has {", ".join(FIELDS)})')
    end = (data or {}).get('billingCycleEnd')
    reset = datetime.fromtimestamp(int(end) / 1000, timezone.utc) if str(end or '').isdigit() else None
    return round(float(v), 1), reset
