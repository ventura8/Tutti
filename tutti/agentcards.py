"""Agent inventory shared by the dashboard and its terminal view."""

from collections import Counter, defaultdict

from . import config, fleet, jobprogress, settings

FIELDS = {'roles': 'Roles', 'capabilities': 'Capabilities', 'limits': 'Limits', 'live_state': 'Live state',
          'learned': 'Learned strengths and limits', 'benchmarks': 'Benchmark scores', 'cost': 'Cost', 'actions': 'Admin actions'}
METERS = ('window', 'used', 'reset', 'panic_at', 'credits_used')


def selected(data, keys):
    return {k: data[k] for k in keys if data.get(k) is not None}


def inventory(cfg, snapshot):
    """One card per account, unmetered worker, scout, and machine/model pair."""
    workers = {w['name']: w for w in snapshot['workers']['items']}
    scouts = snapshot['scouts']['items']
    accounts = {a['name']: a for a in snapshot['usage']['accounts']}
    cards = online_cards(cfg, snapshot, accounts, workers)
    cards += [scout(cfg, snapshot, s, accounts.get(s['account'], {})) for s in scouts]
    cards += local_cards(cfg, snapshot, workers)
    cards += [disabled(o) for o in cfg.get('off', [])]
    return sorted(cards, key=lambda c: (c['kind'] == 'ollama', c['name'].casefold()))


def online_cards(cfg, s, accounts, workers):
    covered = {(a or {}).get('worker') or n for n, a in cfg['accounts'].items()}
    scouted = {row['account'] for row in s['scouts']['items'] if row['name'] == row['account']}
    return ([online(cfg, s, n, workers, a) for n, a in accounts.items() if n not in scouted]
            + [online(cfg, s, n, workers) for n, w in cfg['workers'].items() if w['kind'] != 'ollama' and n not in covered])


def outcomes(tasks):
    counts = defaultdict(Counter)
    for task in tasks:
        attempts = task.get('attempts')
        if not attempts:
            continue
        kind = fleet.kind_of(task)
        for attempt in attempts:
            key = outcome_key(attempt)
            if key:
                counts[key][kind, bool(attempt.get('ok'))] += 1
    return {name: {kind: f'{tally[kind, True]} of {tally[kind, True] + tally[kind, False]} passed'
                   for kind in sorted({k for k, _ in tally})} for name, tally in counts.items()}


def outcome_key(attempt):
    worker, model = attempt.get('worker'), attempt.get('model')
    return (worker, model) if worker and model else attempt.get('step') or worker


def attach_outcomes(cards, tasks):
    history = outcomes(tasks)
    for card in cards:
        key = (card.get('model_worker'), card['name']) if card['kind'] == 'ollama' else card.get('account') or card['name']
        if recorded := history.get(key):
            card['learned'] = {'Backlog attempts': recorded, 'Configured notes': card.get('learned')}
    return cards


def roles(cfg, name, worker, main):
    inferred = ('Worker', 'Main / conductor', 'Reviewer')
    active = (bool(worker), name in (main['agent'], main['configured']), name in ((cfg.get('review') or {}).get('fallback') or []))
    return list(dict.fromkeys([*labels(worker.get('roles')), *(r for r, on in zip(inferred, active, strict=True) if on)]))


def labels(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def mapping(value):
    return value if isinstance(value, dict) else {}


def metadata(worker, account):
    fields = {k: account.get(k, worker.get(k)) for k in ('capabilities', 'learned')}
    fields['benchmarks'] = benchmark_rows(account.get('benchmarks', worker.get('benchmarks')))
    return settings.redact(fields)


def benchmark_rows(rows):
    if not isinstance(rows, list):
        return []
    return [selected(row, ('name', 'score', 'source')) for row in rows if sourced_score(row)]


def sourced_score(row):
    return (isinstance(row, dict) and isinstance(row.get('source'), str)
            and row['source'].startswith(('https://', 'http://')) and type(row.get('score')) in (int, float))


def jobs(snapshot, key, name):
    active = [j for j in snapshot['jobs']['items'] if j['status'] in jobprogress.ACTIVE
              and (j.get('facts', {}).get(key) or j.get(key)) == name]
    estimates = [(j.get('progress') or {}).get('eta_seconds') for j in active]
    return {'running': len(active), 'ETA (seconds)': [eta for eta in estimates if eta is not None]}


def quota(meter):
    meter = meter or {}
    windows = [{**selected(row, ('name', 'verdict')), **selected(row, METERS)} for row in meter.get('limits') or []]
    return {**selected(meter, METERS), **({'Additional windows': windows} if windows else {})}


def online(cfg, s, name, workers, meter=None):
    account = cfg['accounts'].get(name) or {}
    worker_name = account.get('worker') or name
    worker = cfg['workers'].get(worker_name) or {}
    probed = workers.get(worker_name, {})
    limits = {**mapping(worker.get('limits')), **mapping(account.get('limits_display')), **quota(meter)}
    context = worker.get('context_tokens')
    if context:
        limits['Context size'] = context
    paid = worker.get('costs_money') or account.get('costs_money')
    return {**metadata(worker, account), 'name': name, 'kind': worker.get('kind') or 'account', 'worker': worker_name if worker else None,
            'roles': roles(cfg, worker_name, worker, s['main']), 'limits': settings.redact(limits),
            'live_state': {**selected(probed, ('ready',)), **selected(meter or {}, ('verdict', 'stale')),
                           **jobs(s, 'account' if meter is not None else 'worker', name)},
            'cost': billing(paid, meter), 'actions': online_actions(name, worker_name, worker, meter)}


def billing(paid, meter):
    if paid:
        return 'Pay per use'
    return 'Paced subscription' if meter is not None else None


def online_actions(name, worker_name, worker, meter):
    actions = {'Retry ladder': 'autopilot.ladder'} if meter is not None else {}
    if meter is not None:
        actions['Account settings'] = f'accounts.{name}.enabled'
    if worker:
        actions.update({'Enable or disable': f'workers.{worker_name}.enabled',
                        'Edit roles': f'workers.{worker_name}.roles', 'Worker order': 'routing.workers'})
    return actions


def scout(cfg, s, row, meter):
    name = row['name']
    sc = cfg['scouts'][name] or {}
    card = online(cfg, s, row['account'] or name, {}, meter or None)
    return {**card, **metadata(sc, {}), 'name': name, 'account': row['account'], 'kind': row['kind'],
            'roles': ['Scout', *labels(sc.get('roles'))], 'capabilities': row['skills'], 'worker': None,
            'live_state': selected(row, ('ready', 'waiting', 'queued', 'plan')),
            'actions': {'Scout settings': 'scouts', **card['actions']}}


def model_cards(cfg, snapshot, workers):
    """Yield candidates from local workers and live machine inventories."""
    for row in snapshot['fleet']['workers']:
        worker = cfg['workers'][row['name']]
        models = {m['name']: m for m in row['models']}
        if row['model']:
            models.setdefault(row['model'], fleet.classify({'name': row['model']}) or {})
        machine = config.physical(cfg, row['machine']) or 'this computer'
        for name, model in models.items():
            yield (machine, name), (worker, model, workers.get(row['name'], {}), row['name'])


def local_cards(cfg, s, workers):
    candidates = {}
    for key, detail in model_cards(cfg, s, workers):
        if key not in candidates or detail[0].get('model') == key[1]:
            candidates[key] = detail
    energy = {m['name']: m for m in s['local_models']['cards']}
    for machine, row in energy.items():
        for name in filter(None, row['installed']):
            model = fleet.classify({'name': name}) or {}
            candidates.setdefault((machine, name), ({}, model, {}, ''))
    return [local(cfg, s, key, detail, energy.get(key[0], {})) for key, detail in candidates.items()]


def local(cfg, s, key, detail, machine):
    host, name = key
    worker, model, probe, worker_name = detail
    learned = metadata(worker, {})
    configured = name == worker.get('model')
    if not configured:
        learned['benchmarks'] = []
    if model.get('rank_why'):
        learned['learned'] = {'Fleet evidence': model['rank_why'], 'Configured notes': learned['learned']}
    limit = selected({'Context size': worker.get('num_ctx') or worker.get('context_tokens')}, ('Context size',))
    return {**learned, 'name': name, 'machine': host, 'kind': 'ollama', 'worker': worker_name if configured else None,
            'model_worker': worker_name or None,
            'roles': roles(cfg, worker_name, worker, s['main']),
            'capabilities': ['Runs locally', *(model.get('kinds') or []), *labels(worker.get('capabilities'))],
            'limits': settings.redact({**mapping(worker.get('limits')), **limit}),
            'live_state': {**selected(probe, ('ready',)), **selected(machine, ('status',)),
                           'loaded': any(m['name'] == name for m in machine.get('loaded', [])), **jobs(s, 'model', name)},
            'cost': {'Billing': 'Electricity only', 'Machine watts': machine.get('watts'),
                     'Machine cost per job': machine.get('per_job')},
            'actions': local_actions(worker, worker_name)}


def local_actions(worker, name):
    links = {'Local model settings': 'fleet'}
    if worker and not worker.get('auto'):
        links.update(online_actions(name, name, worker, None))
    return links


def disabled(entry):
    kind = entry['kind']
    paused = bool(entry.get('paused'))
    field = 'paused' if paused else 'enabled'
    return {'name': entry['name'], 'kind': kind, 'live_state': {'status': 'Paused' if paused else 'Disabled'},
            'cost': 'Pay per use (off)' if entry['costs_money'] else None,
            'actions': {'Enable or resume': f'{kind}s.{entry["name"]}.{field}'}}


def describe(value):
    if isinstance(value, list):
        return ', '.join(map(describe, value)) or 'Unavailable'
    if isinstance(value, dict):
        return ', '.join(f'{k} — {describe(v)}' for k, v in value.items()) or 'Unavailable'
    return str(value) if value is not None else 'Unavailable'


def lines(cards):
    out = ['Agents']
    for card in cards:
        label = ' · '.join(filter(None, (card['name'], card.get('machine') or card.get('account'))))
        out.extend(['', label, *(f'  {label}: {describe(card.get(field))}' for field, label in FIELDS.items())])
    return out if cards else [*out, 'No agents configured.']
