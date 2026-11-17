"""Linear two-way sync (docs/INTEGRATIONS.md, *Linear*): open Linear issues with the sync label, in the mapped teams, become
backlog tasks of the mapped repo; a linked task's progress goes back to its issue (state and a short comment), and its pull
request is attached. An issue closed or cancelled in Linear closes its task while no run holds it.

Off by default (`integrations.linear.enabled`). The personal API key is read at the point of use from
~/.config/tutti/integrations.env (`tutti integrations linear setup`), never from the config, never printed. Polling every
`poll_minutes` is the default; Linear's webhook (optional) only makes the next sync start at once.

Pure core (import_issues, apply_remote, pending_pushes, pick_state, comment_text); the GraphQL call is the thin edge."""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

from . import backlog, config, durable, secretenv

ENDPOINT = 'https://api.linear.app/graphql'
KEY_ENV = 'TUTTI_LINEAR_KEY'
WEBHOOK_ENV = 'TUTTI_LINEAR_WEBHOOK_SECRET'
STATE_FILE = 'integrations.json'
DEFAULTS = {'enabled': False, 'key_env': KEY_ENV, 'label': 'tutti', 'teams': {}, 'projects': {}, 'poll_minutes': 10,
            'comment': True, 'states': {}}
# a task's status -> the Linear workflow state type it moves the issue to
STATE_TYPE = {'queued': 'unstarted', 'claimed': 'started', 'running': 'started', 'review': 'started', 'needs-person': 'started',
              'done': 'completed', 'dropped': 'canceled'}
SAY = {'queued': 'queued again', 'claimed': 'claimed by a session', 'running': 'a worker is on it', 'review': 'passed its check; '
       'waiting for review', 'needs-person': 'needs a person\'s answer (the dashboard\'s Needs tab)', 'done': 'done',
       'dropped': 'dropped from the backlog'}
FIELDS = 'id identifier title description url team { id key } project { name } state { type }'
OPEN_Q = ('query($teams: [String!], $label: String!) { issues(first: 100, filter: {team: {key: {in: $teams}}, labels: {some: {name: '
          '{eqIgnoreCase: $label}}}, state: {type: {nin: ["completed", "canceled"]}}}) { nodes { ' + FIELDS + ' } } }')
IDS_Q = 'query($ids: [ID!]) { issues(first: 100, filter: {id: {in: $ids}}) { nodes { ' + FIELDS + ' } } }'
STATES_Q = 'query($team: ID!) { workflowStates(filter: {team: {id: {eq: $team}}}) { nodes { id name type position } } }'
UPDATE_M = 'mutation($id: String!, $state: String!) { issueUpdate(id: $id, input: {stateId: $state}) { success } }'
COMMENT_M = 'mutation($id: String!, $body: String!) { commentCreate(input: {issueId: $id, body: $body}) { success } }'
LINK_M = 'mutation($id: String!, $url: String!) { attachmentLinkURL(issueId: $id, url: $url) { success } }'
VIEWER_Q = 'query { viewer { id } }'


class LinearError(Exception):
    """A Linear call or setting failed; the message names the step, the cause and the fix (never the key)."""


def settings(cfg):
    s = ((cfg.get('integrations') or {}).get('linear') or {})
    return {**DEFAULTS, **(s if isinstance(s, dict) else {})}


def errors(cfg):
    """Config problems in integrations.linear, each with its fix."""
    raw = (cfg.get('integrations') or {}).get('linear')
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [f'integrations.linear is {raw!r}: a mapping such as {{enabled: true, teams: {{ENG: app}}}}']
    s, errs = settings(cfg), []
    for k in ('teams', 'projects', 'states'):
        if not isinstance(s[k], dict):
            errs.append(f'integrations.linear.{k} is {s[k]!r}: a mapping (teams: {{ENG: app}})')
    repos = ((cfg.get('autopilot') or {}).get('repos') or {})
    for k in ('teams', 'projects'):
        for name, repo in (s[k] if isinstance(s[k], dict) else {}).items():
            if repos and repo not in repos:
                errs.append(f'integrations.linear.{k}.{name} maps to "{repo}", which is not under autopilot.repos: name one of '
                            f'{", ".join(sorted(repos))}')
    if s['enabled'] and not s['teams']:
        errs.append('integrations.linear is enabled with no teams: add teams: {<TEAM KEY>: <repo>} (the key is the issue prefix, ENG in ENG-12)')
    if not isinstance(s['poll_minutes'], (int, float)) or s['poll_minutes'] < 1:
        errs.append(f'integrations.linear.poll_minutes is {s["poll_minutes"]!r}: a number of minutes, 1 or more')
    if not str(s['label']).strip():
        errs.append('integrations.linear.label is empty: the label an issue needs to become a task (default tutti)')
    return errs


def env_path(env=None):
    return os.path.join(config.xdg_tutti_dir(env), secretenv.INTEGRATIONS_FILE)


def api_key(cfg, env=None):
    return secretenv.get(settings(cfg)['key_env'], env).strip()


# ---- the thin edge

def urlopen_post(key, body, timeout=20):
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'Authorization': key, 'User-Agent': 'tutti'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:   # a fixed https address of Linear's
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Client:
    """GraphQL calls with the key; `post(key, body) -> (status, bytes)` is replaced in tests."""

    def __init__(self, key, post=None):
        if not key:
            raise LinearError(f'no Linear API key: run `tutti integrations linear setup` (it writes {KEY_ENV} to '
                              '~/.config/tutti/integrations.env, mode 600)')
        self._key, self._post = key, post or urlopen_post

    def q(self, query, variables=None):
        try:
            status, raw = self._post(self._key, {'query': query, 'variables': variables or {}})
        except OSError as e:
            raise LinearError(f'Linear could not be reached ({type(e).__name__}): check the network; the next sync tries '
                              'again') from None
        if status in (401, 403):
            raise LinearError(f'Linear refused the key (HTTP {status}): create a new personal API key and run '
                              '`tutti integrations linear setup` again')
        try:
            out = json.loads(raw or b'{}')
        except ValueError:
            out = {}
        if status != 200 or out.get('errors'):
            what = '; '.join(str(e.get('message', ''))[:200] for e in out.get('errors') or []) or f'HTTP {status}'
            raise LinearError(f'Linear answered an error ({what}): check integrations.linear (teams, label) and the key\'s access')
        return out.get('data') or {}


def fetch_open(client, s):
    return client.q(OPEN_Q, {'teams': sorted(s['teams']), 'label': str(s['label'])})['issues']['nodes']


def fetch_by_ids(client, ids):
    out = []
    ids = sorted(ids)
    for i in range(0, len(ids), 100):
        out += client.q(IDS_Q, {'ids': ids[i:i + 100]})['issues']['nodes']
    return out


# ---- the pure core

def repo_for(issue, s):
    project = ((issue.get('project') or {}).get('name')) or ''
    if project in s['projects']:
        return s['projects'][project]
    return s['teams'].get(((issue.get('team') or {}).get('key')) or '')


def linked(data):
    return {t['linear']['id']: t for t in data['tasks'] if isinstance(t.get('linear'), dict) and t['linear'].get('id')}


def import_issues(data, issues, s):
    """New tasks for the issues no task is linked to yet; returns the tasks added. The title starts with the identifier so
    a branch, commit or pull request that names it is linked by Linear's GitHub integration too."""
    have, added = linked(data), []
    for i in issues:
        repo = repo_for(i, s)
        if not repo or i['id'] in have:
            continue
        notes = [f'Linear {i["identifier"]}: {i["url"]}']
        if (i.get('description') or '').strip():
            notes.append(i['description'].strip()[:4000])
        task, new = backlog.add(data, backlog.new_task(repo, f'{i["identifier"]} {i["title"]}', source='linear', notes=notes))
        task['linear'] = {'id': i['id'], 'identifier': i['identifier'], 'url': i['url'], 'team': i['team']['id'],
                          'pushed': task['status'] if new else None}
        if new:
            added.append(task)
    return added


def apply_remote(data, issues):
    """An issue completed or cancelled in Linear closes its task, unless a run or a session holds it; returns the tasks closed."""
    by_id, closed = linked(data), []
    for i in issues:
        t = by_id.get(i['id'])
        kind = (i.get('state') or {}).get('type')
        if t and kind in ('completed', 'canceled') and t['status'] in ('queued', 'candidate', 'needs-person'):
            t.update(status='done' if kind == 'completed' else 'dropped', closed_at=time.time())
            t['linear']['pushed'] = t['status']
            t['notes'] = [*t.get('notes', []), f'closed in Linear ({kind})']
            closed.append(t)
    return closed


def pending_pushes(data):
    """[(task id, status)] for the linked tasks whose status changed since Linear last heard."""
    return [(t['id'], t['status']) for t in linked(data).values() if t['status'] in STATE_TYPE and t['linear'].get('pushed') != t['status']]


def pick_state(states, status, s):
    """The workflow state id for `status`: integrations.linear.states.<status> by name, else the first of its type."""
    want = (s.get('states') or {}).get(status)
    if want:
        hit = [x for x in states if x['name'].lower() == str(want).lower()]
        if hit:
            return hit[0]['id']
    kind = STATE_TYPE.get(status)
    hit = sorted((x for x in states if x['type'] == kind), key=lambda x: x.get('position') or 0)
    return hit[0]['id'] if hit else None


def comment_text(task, status):
    out = f'Tutti: {SAY.get(status, status)} (task {task["id"]}).'
    if task.get('pr'):
        out += f' Pull request: {task["pr"]}'
    return out


def verify_webhook(secret, body, signature):
    """True when `signature` (Linear-Signature header) is the HMAC-SHA256 of the raw body with the webhook's secret."""
    if not secret or not signature:
        return False
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, str(signature).strip())


# ---- the round

def state_path(cfg):
    return os.path.join(config.state_dir(cfg), STATE_FILE)


def read_state(cfg):
    try:
        with open(state_path(cfg), encoding='utf8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    return d if isinstance(d, dict) else {}


def write_state(cfg, part, values):
    d = read_state(cfg)
    d[part] = {**(d.get(part) or {}), **values}
    os.makedirs(os.path.dirname(state_path(cfg)), exist_ok=True)
    durable.dump_json(state_path(cfg), d, indent=1)


def wake(cfg):
    """A verified webhook: the next tick syncs at once."""
    write_state(cfg, 'linear', {'wake': True})


def push(client, task, status, s, states_cache):
    lin = task['linear']
    if lin['team'] not in states_cache:
        states_cache[lin['team']] = client.q(STATES_Q, {'team': lin['team']})['workflowStates']['nodes']
    sid = pick_state(states_cache[lin['team']], status, s)
    if sid:
        client.q(UPDATE_M, {'id': lin['id'], 'state': sid})
    if s['comment'] and status != 'queued':
        client.q(COMMENT_M, {'id': lin['id'], 'body': comment_text(task, status)})
    if task.get('pr') and lin.get('pr') != task['pr']:
        client.q(LINK_M, {'id': lin['id'], 'url': task['pr']})


def sync(cfg, client):
    """One sync: import, apply Linear's closes, push changes. Network outside the backlog's lock. Returns a report."""
    s = settings(cfg)
    issues = fetch_open(client, s)
    with backlog.locked(cfg) as data:
        ids = set(linked(data))
    remote = fetch_by_ids(client, ids - {i['id'] for i in issues}) if ids else []
    with backlog.locked(cfg) as data:
        added = import_issues(data, issues, s)
        closed = apply_remote(data, remote)
        pushes = pending_pushes(data)
        tasks = {t['id']: dict(t, linear=dict(t['linear'])) for t in data['tasks'] if t['id'] in {p[0] for p in pushes}}
    done, cache, failed = [], {}, []
    for tid, status in pushes:
        try:
            push(client, tasks[tid], status, s, cache)
            done.append((tid, status))
        except LinearError as e:
            failed.append(f'{tid}: {e}')
    if done:
        with backlog.locked(cfg) as data:
            for tid, status in done:
                t = backlog.find(data, tid)
                t['linear'].update(pushed=status, pr=tasks[tid].get('pr') or t['linear'].get('pr'))
    return {'imported': [t['id'] for t in added], 'closed': [t['id'] for t in closed], 'pushed': [d[0] for d in done],
            'failed': failed}


def due(cfg, now):
    s = settings(cfg)
    st = read_state(cfg).get('linear') or {}
    return bool(s['enabled']) and (st.get('wake') or now - float(st.get('last') or 0) >= float(s['poll_minutes']) * 60)


def tick(cfg, now=None, client=None, env=None):
    """The dashboard's round: a sync when it is due; an error is kept for `tutti integrations linear status`, never raised."""
    now = now or time.time()
    if not due(cfg, now):
        return None
    try:
        rep = sync(cfg, client or Client(api_key(cfg, env)))
        write_state(cfg, 'linear', {'last': now, 'wake': False, 'error': '; '.join(rep['failed']) or None,
                                    'report': {k: len(v) for k, v in rep.items()}})
        return rep
    except (LinearError, backlog.BacklogError, KeyError, TypeError) as e:
        why = str(e) if isinstance(e, (LinearError, backlog.BacklogError)) else f'Linear\'s answer was not understood ({type(e).__name__})'
        write_state(cfg, 'linear', {'last': now, 'wake': False, 'error': why})
        return {'error': why}
