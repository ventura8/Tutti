"""The Admin tab (docs/ADMIN.md): what the household spends, per provider and account, this month and month by month, with a
forecast, and every admin control in one place.

Costs come from the config only (costs.subscriptions: a fixed monthly fee; costs.metered: pay-per-use spent this month) and
from the Local models' electricity estimate per machine (tutti/energy.py). Tutti never reads a key or a bill: anything it
cannot know is a row marked "not configured" that says which setting to fill. Pure: the snapshot and the config in, a dict
out; the one write is the month history in the state folder (record)."""
import calendar
import json
import os
import time

from . import config, energy, tz

HISTORY_FILE = 'costs-history.json'
KEEP_MONTHS = 24
HOW_SUB = 'set costs.subscriptions.{name}.monthly (Settings → Costs) to its monthly fee'
HOW_METERED = 'set costs.metered.{name}.month_to_date (Settings → Costs) from its billing page'


def convert(amount, frm, to):
    """amount in `frm` as `to` through energy.PER_USD; None when either currency is unknown."""
    if amount is None:
        return None
    if frm == to:
        return amount
    a, b = energy.PER_USD.get(frm), energy.PER_USD.get(to)
    return None if not a or not b else amount / a * b


def month_info(now):
    """(month key 'YYYY-MM', day of month, days in month) in the household's local time."""
    lt = tz.localtime(now)
    return f'{lt.tm_year:04d}-{lt.tm_mon:02d}', lt.tm_mday, calendar.monthrange(lt.tm_year, lt.tm_mon)[1]


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def _usage(accounts, name):
    a = accounts.get(name) or {}
    used = a.get('used')
    return {'used_pct': used, 'verdict': a.get('verdict') or 'no reading', 'reset': a.get('reset')}


def _paid_workers(cfg):
    """Workers and accounts that cost money per use and are turned on (Copilot when enabled)."""
    out = []
    for kind in ('workers', 'accounts'):
        for n, w in (cfg.get(kind) or {}).items():
            w = w or {}
            if w.get('costs_money') and w.get('enabled'):
                out.append(n)
    return out


def rows(cfg, accounts, energy_cards, now):
    """Every cost line: {name, kind, provider, monthly, mtd, forecast, currency, configured, how, usage}."""
    c = cfg.get('costs') or {}
    cur = c.get('currency') or energy.tariff(cfg)['currency']
    _m, day, days = month_info(now)
    subs, metered = c.get('subscriptions') or {}, c.get('metered') or {}
    out = []
    for n, s in subs.items():
        s = s or {}
        fee = convert(_num(s.get('monthly')), s.get('currency') or cur, cur)
        out.append({'name': n, 'kind': 'subscription', 'provider': s.get('provider') or n, 'monthly': fee,
                    'mtd': None if fee is None else fee * day / days, 'forecast': fee, 'configured': fee is not None,
                    'how': '' if fee is not None else HOW_SUB.format(name=n), 'usage': _usage(accounts, n)})
    for n, s in metered.items():
        s = s or {}
        mtd = convert(_num(s.get('month_to_date')), s.get('currency') or cur, cur)
        out.append({'name': n, 'kind': 'metered', 'provider': s.get('provider') or n, 'monthly': None, 'mtd': mtd,
                    'forecast': None if mtd is None else mtd / day * days, 'configured': mtd is not None,
                    'how': '' if mtd is not None else HOW_METERED.format(name=n), 'usage': _usage(accounts, n)})
    known = set(subs) | set(metered)
    for n, a in (cfg.get('accounts') or {}).items():
        if n in known or (a or {}).get('enabled') is False:
            continue
        out.append({'name': n, 'kind': 'subscription', 'provider': (a or {}).get('worker') or n, 'monthly': None, 'mtd': None,
                    'forecast': None, 'configured': False, 'how': HOW_SUB.format(name=n), 'usage': _usage(accounts, n)})
    for n in _paid_workers(cfg):
        if n not in known and n not in (cfg.get('accounts') or {}):
            out.append({'name': n, 'kind': 'metered', 'provider': n, 'monthly': None, 'mtd': None, 'forecast': None,
                        'configured': False, 'how': HOW_METERED.format(name=n), 'usage': _usage(accounts, n)})
    for card in energy_cards or []:
        m = (card.get('periods') or {}).get('month') or {}
        mtd = convert(_num(m.get('cost')), energy.tariff(cfg)['currency'], cur)
        out.append({'name': f'electricity: {card.get("name")}', 'kind': 'electricity', 'provider': 'electricity', 'monthly': None,
                    'mtd': mtd, 'forecast': None if mtd is None else mtd / day * days, 'configured': mtd is not None,
                    'measured': bool(card.get('measured')), 'kwh': m.get('kwh'),
                    'how': '' if mtd is not None else 'no power reading yet from this machine (docs/ENERGY.md)', 'usage': None})
    for r in out:
        for k in ('monthly', 'mtd', 'forecast'):
            r[k] = None if r[k] is None else round(r[k], 2)
    return out, cur


def totals(items):
    """{mtd, forecast, unknown}: sums over the configured rows and how many rows are not configured."""
    def total(k):
        return round(sum(r[k] for r in items if r[k] is not None), 2)
    return {'mtd': total('mtd'), 'forecast': total('forecast'), 'unknown': sum(1 for r in items if not r['configured'])}


def load_history(cfg):
    try:
        with open(os.path.join(config.state_dir(cfg), HISTORY_FILE)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(cfg, month, tot, currency, history=None):
    """The month's running total into the history (the last answer of a month is its total); returns the history."""
    h = dict(load_history(cfg) if history is None else history)
    entry = {'total': tot['mtd'], 'forecast': tot['forecast'], 'currency': currency}
    if h.get(month) == entry:
        return h
    h[month] = entry
    for old in sorted(h)[:-KEEP_MONTHS]:
        del h[old]
    path = os.path.join(config.state_dir(cfg), HISTORY_FILE)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(h, f)
        os.replace(tmp, path)
    except OSError:
        pass   # the history is a convenience: the tab still shows this month
    return h


def controls(snap):
    """The admin controls gathered from the dashboard and Settings: {name, state, href}."""
    s = snap or {}
    fo = s.get('failover') or {}
    up = s.get('updates') or {}
    opt = (s.get('controls') or {}).get('opt_in') or []
    needs = s.get('needs') or {}
    machines = (s.get('machines') or {}).get('items') or []
    limited = [m['name'] for m in machines if m.get('limit')]
    return [
        {'name': 'Project priorities', 'state': 'projects.<name>.priority', 'href': '#settings/projects'},
        {'name': 'Machine limits and hours', 'state': ', '.join(limited) or 'no machine limited', 'href': '#dashboard'},
        {'name': 'Night mode', 'state': 'see Devices', 'href': '#settings/devices'},
        {'name': 'Backup main machine', 'state': fo.get('backup') or 'not set', 'href': '#settings/machines'},
        {'name': 'GitHub connection', 'state': 'your GitHub card', 'href': '#dashboard'},
        {'name': 'Accounts', 'state': 'quotas and readings', 'href': '#settings/accounts'},
        {'name': 'Opt-in and paid workers', 'state': ', '.join(f'{o.get("name")}: {"on" if o.get("on") else "off"}' for o in opt) or 'none',
         'href': '#settings/optins'},
        {'name': 'Self-update', 'state': up.get('current') or up.get('version') or 'see Updates', 'href': '#settings/update'},
        {'name': 'Needs your input', 'state': f'{len(needs.get("items") or [])} waiting' if needs else 'see Needs you', 'href': '#dashboard'},
        {'name': 'Users and invites', 'state': 'people and roles', 'href': '#users'},
    ]


def view(cfg, snap, now=None, write=True):
    """GET /api/admin (admins only): costs per row, totals, the month history, and the controls."""
    now = time.time() if now is None else now
    s = snap or {}
    accounts = {a['name']: a for a in ((s.get('usage') or {}).get('accounts') or [])}
    items, cur = rows(cfg, accounts, (s.get('local_models') or {}).get('cards'), now)
    tot = totals(items)
    month, day, days = month_info(now)
    history = record(cfg, month, tot, cur) if write else load_history(cfg)
    return {'currency': cur, 'month': month, 'day': day, 'days': days, 'rows': items, 'totals': tot,
            'history': [{'month': k, **v} for k, v in sorted(history.items(), reverse=True)],
            'controls': controls(s), 'method': 'docs/ADMIN.md'}
