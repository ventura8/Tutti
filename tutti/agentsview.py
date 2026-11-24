"""The Agents tab (t252, docs/DASHBOARD.md, *Agents*): one card per agent Tutti can use (a worker, an account without a
worker, a scout), with the roles the config gives it and why, what it can do, its limits and its live state.

Pure: the config and the dashboard's own snapshot in, cards out. Roles come only from the config (no guessing): main and
fallback (`main`), worker (`routing.workers`, the autopilot's accounts and ladder), reviewer (`review.reviewers` and
`review.fallback`), decisional model (`decisional.candidates`), Chat (every worker that answers), scout (`scouts`)."""

ROLE_LABELS = {'main': 'Main agent', 'worker': 'Worker', 'reviewer': 'Reviewer', 'decisional': 'Decisional model', 'chat': 'Chat',
               'scout': 'Scout'}


def _names(value):
    return [str(v) for v in value] if isinstance(value, list) else []


def roles(cfg, name, worker, account):
    """[{role, label, why}] the config gives this agent (by its worker name or any of its accounts: one, or a list)."""
    keys = {k for k in (name, *(account if isinstance(account, (list, tuple)) else [account])) if k}
    out = []
    main = cfg.get('main') or {}
    order = [main.get('preferred'), *_names(main.get('fallback'))]
    hit = next((i for i, n in enumerate(order) if n in keys), None)
    if hit is not None:
        out.append(('main', 'main.preferred' if hit == 0 else f'main.fallback, number {hit}'))
    ap = cfg.get('autopilot') or {}
    picks = set(_names((cfg.get('routing') or {}).get('workers'))) | set(_names(ap.get('accounts'))) | set(_names(ap.get('ladder')))
    if keys & picks:
        out.append(('worker', 'routing.workers or the autopilot\'s accounts and ladder'))
    review = cfg.get('review') or {}
    if keys & (set(_names(review.get('reviewers'))) | set(_names(review.get('fallback')))):
        out.append(('reviewer', 'review.reviewers or review.fallback'))
    model = (worker or {}).get('model')
    if model and model in _names((cfg.get('decisional') or {}).get('candidates')):
        out.append(('decisional', f'decisional.candidates lists {model}'))
    if worker and worker.get('kind') in ('ollama', 'cli', 'openai_api'):
        out.append(('chat', 'answers in Chat when Auto or a person picks it'))
    return [{'role': r, 'label': ROLE_LABELS[r], 'why': why} for r, why in out]


def capabilities(worker):
    w = worker or {}
    out = {'ollama': ['local model', 'native tool calls'], 'cli': ['coding agent (CLI)', 'tools through Tutti\'s ACTION lines'],
           'openai_api': ['online API', 'native tool calls']}.get(w.get('kind'), [])
    if w.get('vision'):
        out.append('reads images')
    ctx = w.get('num_ctx') or w.get('context_tokens')
    if ctx:
        out.append(f'context {int(ctx):,} tokens')
    return out


def cost(cfg, worker, accs):
    """What using this agent costs, from the config: pay per use (and whether it is still off), an API's tokens, a local
    model's electricity, or a subscription Tutti paces. '' when the config says nothing about it."""
    w, accounts = worker or {}, cfg.get('accounts') or {}
    paid = w.get('costs_money') or any((accounts.get(n) or {}).get('costs_money') for n in accs)
    if paid:
        on = w.get('enabled') or any((accounts.get(n) or {}).get('enabled') for n in accs)
        return 'pays per use' + ('' if on else ': kept off until enabled: true')
    if w.get('kind') == 'ollama':
        return 'electricity only (estimated under Usage & pacing)'
    if w.get('kind') == 'openai_api':
        return 'pays per token (API)'
    return 'subscription: no cost per task, paced to its window' if accs else ''


def _limits(acc):
    if not acc:
        return []
    out = []
    used = acc.get('used')
    if isinstance(used, (int, float)):
        out.append(f'{used:.0f} % used' + (f' ({acc["verdict"]})' if acc.get('verdict') else ''))
    if acc.get('reset'):
        out.append(f'resets {acc["reset"]}')
    if acc.get('panic'):
        out.append('panic: kept for the main agent')
    if acc.get('reader_problem'):
        out.append(f'reading: {acc["reader_problem"]}')
    return out


def _running(snap, *names):
    """Running jobs of this agent, each counted once: its worker or any of its accounts (jobs keep them under `facts`)."""
    keys = {k for k in names if k}
    count = 0
    for j in ((snap.get('jobs') or {}).get('items') or []):
        f = j.get('facts') or {}
        if j.get('status') == 'running' and keys & {j.get('worker'), j.get('account'), f.get('worker'), f.get('account')} - {None}:
            count += 1
    return count


def accounts_of(cfg, name):
    """The accounts a worker spends: the one of its name, and every account whose `worker` names it."""
    accs = cfg.get('accounts') or {}
    own = [n for n, a in accs.items() if n == name or (a or {}).get('worker') == name]
    return own + [x for n in own for x in _names((accs[n] or {}).get('limits')) if x in accs and x not in own]   # its weekly bounds


def track_records(tasks):
    """{step: {runs, passed, kinds: {class: n}, why: [top failure reasons], sizes: {size: [runs, passed]}}} from every
    attempt in the backlog: what each agent has actually done (t252's learned strengths and limits)."""
    out = {}
    for t in tasks:
        size = t.get('size') or '?'
        for a in t.get('attempts') or []:
            if not isinstance(a, dict) or not a.get('step'):
                continue
            r = out.setdefault(a['step'], {'runs': 0, 'passed': 0, 'kinds': {}, 'why': {}, 'sizes': {}})
            ok = bool(a.get('ok'))
            r['runs'] += 1
            r['passed'] += ok
            s = r['sizes'].setdefault(size, [0, 0])
            s[0] += 1
            s[1] += ok
            if not ok:
                kind = a.get('class') or 'unknown'
                r['kinds'][kind] = r['kinds'].get(kind, 0) + 1
                why = ' '.join(str(a.get('why') or 'no reason given').split())[:70]
                r['why'][why] = r['why'].get(why, 0) + 1
    for r in out.values():
        r['why'] = [w for w, _n in sorted(r['why'].items(), key=lambda x: -x[1])[:3]]
    return out


def record_line(records, names):
    """The card's one line for an agent's own steps (its name and its accounts), or '' when it never ran."""
    rs = [records[n] for n in names if n in records]
    runs, passed = sum(r['runs'] for r in rs), sum(r['passed'] for r in rs)
    if not runs:
        return ''
    kinds = {}
    for r in rs:
        for k, n in r['kinds'].items():
            kinds[k] = kinds.get(k, 0) + n
    top = max(kinds, key=kinds.get) if kinds else ''
    line = f'{runs} runs, {passed} passed ({100 * passed / runs:.0f} %)'
    if top:
        line += f'; most failures: {top} ({kinds[top]})'
    return line


_RECORDS = {}   # backlog file -> ((mtime_ns, size), records): read once per change of the backlog


def records_of(cfg):
    import os

    from . import backlog, config
    path = os.path.join(config.state_dir(cfg), 'backlog.json')
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    hit = _RECORDS.get(path)
    if hit and hit[0] == key:
        return hit[1]
    recs = track_records(backlog.load(cfg)['tasks'])
    _RECORDS[path] = (key, recs)
    return recs


def main_marks(snap, name):
    """{'main_now', 'main_preferred', 'main_by'} for one agent from the dashboard's `main` block: the Agents tab says which
    card is the main agent now, and whether the owner pinned it or Tutti put it there while the preferred one is out."""
    m = snap.get('main') or {}
    now = bool(m.get('agent')) and m['agent'] == name
    return {'main_now': now, 'main_preferred': bool(m.get('preferred')) and m['preferred'] == name,
            'main_by': ('preferred' if m.get('chosen_by') == 'preferred' else 'auto' if m.get('auto') else m.get('chosen_by') or 'config')
            if now else ''}


def cards(cfg, snap):
    """The Agents tab: workers first (in the config's order) with every account they spend, then accounts no worker or scout
    spends, then scouts."""
    accounts = {a['name']: a for a in ((snap.get('usage') or {}).get('accounts') or [])}
    live = {w['name']: w for w in ((snap.get('workers') or {}).get('items') or [])}
    scouts = {s.get('name'): s for s in ((snap.get('scouts') or {}).get('items') or [])}
    out, spent = [], {(sc or {}).get('account') for sc in (cfg.get('scouts') or {}).values()}
    for name, w in (cfg.get('workers') or {}).items():
        accs = accounts_of(cfg, name)
        spent.update(accs)
        state = live.get(name)
        out.append({'name': name, 'kind': w.get('kind'), 'model': w.get('model') or (w.get('command') or [None])[0],
                    'machine': w.get('machine') or 'this host', 'account': ', '.join(accs) or None,
                    **main_marks(snap, name),
                    'roles': roles(cfg, name, w, accs), 'capabilities': capabilities(w), 'cost': cost(cfg, w, accs),
                    'limits': [f'{n}: {x}' if len(accs) > 1 else x for n in accs for x in _limits(accounts.get(n))],
                    'state': 'checking' if state is None else 'ready' if state.get('ready') else 'not ready',
                    'note': (state or {}).get('note') or '',
                    'running': _running(snap, name, *accs), 'record': record_line(snap.get('_records') or {}, [name, *accs]),
                    'failures': [w for n in (name, *accs) for w in ((snap.get('_records') or {}).get(n) or {}).get('why', [])][:3]})
    for name in cfg.get('accounts') or {}:
        if name in spent:
            continue
        out.append({'name': name, 'kind': 'account', 'model': None, 'machine': None, 'account': name, **main_marks(snap, name),
                    'roles': roles(cfg, name, None, name),
                    'capabilities': ['an account Tutti paces (no worker of its own)'],
                    'cost': cost(cfg, None, [name]), 'limits': _limits(accounts.get(name)),
                    'state': 'paced', 'note': '', 'running': _running(snap, name, name)})
    for name, sc in (cfg.get('scouts') or {}).items():
        sc, seen = sc or {}, scouts.get(name)
        out.append({'name': name, 'kind': 'scout', 'model': sc.get('kind'), 'machine': None, 'account': sc.get('account'),
                    **main_marks(snap, name),
                    'roles': [{'role': 'scout', 'label': ROLE_LABELS['scout'], 'why': f'scouts.{name}'}],
                    'capabilities': [str(x) for x in sc.get('skills') or []],
                    'cost': cost(cfg, None, [sc['account']] if sc.get('account') else []),
                    'limits': _limits(accounts.get(sc.get('account'))),
                    'state': 'checking' if seen is None else 'ready' if seen.get('ready') else 'not ready',
                    'note': '' if not seen or seen.get('ready') else f'missing {", ".join(seen.get("missing") or [])}', 'running': 0})
    return out
