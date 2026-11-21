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
    """[{role, label, why}] the config gives this agent (by its worker or its account name)."""
    keys = {k for k in (name, account) if k}
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


def _limits(acc):
    if not acc:
        return []
    out = []
    used = acc.get('used')
    if isinstance(used, (int, float)):
        out.append(f'{used:.0f} % used' + (f' ({acc["verdict"]})' if acc.get('verdict') else ''))
    if acc.get('resets'):
        out.append(f'resets {acc["resets"]}')
    if acc.get('panic'):
        out.append('panic: kept for the main agent')
    if acc.get('reader_problem'):
        out.append(f'reading: {acc["reader_problem"]}')
    return out


def _running(snap, name, account):
    keys = {k for k in (name, account) if k}
    return sum(1 for j in ((snap.get('jobs') or {}).get('items') or []) if j.get('status') == 'running'
               and (j.get('worker') in keys or j.get('account') in keys))


def accounts_of(cfg, name):
    """The accounts a worker spends: the one of its name, and every account whose `worker` names it."""
    accs = cfg.get('accounts') or {}
    own = [n for n, a in accs.items() if n == name or (a or {}).get('worker') == name]
    return own + [x for n in own for x in _names((accs[n] or {}).get('limits')) if x in accs and x not in own]   # its weekly bounds


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
        first = accs[0] if accs else None
        state = live.get(name)
        out.append({'name': name, 'kind': w.get('kind'), 'model': w.get('model') or (w.get('command') or [None])[0],
                    'machine': w.get('machine') or 'this host', 'account': ', '.join(accs) or None,
                    'roles': roles(cfg, name, w, first), 'capabilities': capabilities(w),
                    'limits': [f'{n}: {x}' if len(accs) > 1 else x for n in accs for x in _limits(accounts.get(n))],
                    'state': 'checking' if state is None else 'ready' if state.get('ready') else 'not ready',
                    'note': (state or {}).get('note') or '',
                    'running': sum(_running(snap, name, n) for n in accs) or _running(snap, name, None)})
    for name in cfg.get('accounts') or {}:
        if name in spent:
            continue
        out.append({'name': name, 'kind': 'account', 'model': None, 'machine': None, 'account': name, 'roles': roles(cfg, name, None, name),
                    'capabilities': ['an account Tutti paces (no worker of its own)'], 'limits': _limits(accounts.get(name)),
                    'state': 'paced', 'note': '', 'running': _running(snap, name, name)})
    for name, sc in (cfg.get('scouts') or {}).items():
        sc, seen = sc or {}, scouts.get(name)
        out.append({'name': name, 'kind': 'scout', 'model': sc.get('kind'), 'machine': None, 'account': sc.get('account'),
                    'roles': [{'role': 'scout', 'label': ROLE_LABELS['scout'], 'why': f'scouts.{name}'}],
                    'capabilities': [str(x) for x in sc.get('skills') or []], 'limits': _limits(accounts.get(sc.get('account'))),
                    'state': 'checking' if seen is None else 'ready' if seen.get('ready') else 'not ready',
                    'note': '' if not seen or seen.get('ready') else f'missing {", ".join(seen.get("missing") or [])}', 'running': 0})
    return out
