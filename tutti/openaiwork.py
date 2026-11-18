"""`tutti run openai "<task>" --repo <repo>`: OpenAI-only work scheduled like any other task."""
import time
from datetime import datetime, timezone

from . import autopilot, backlog, pacing, state

WORKER = 'openai'
NO_ROOM = ('panic', 'spent')


class OpenAIError(Exception):
    """The task cannot be scheduled; the message says why and what to change."""


def account_of(cfg, worker=WORKER):
    """The account whose runs use the openai worker (accounts.<n>.worker, or the account named openai)."""
    names = [n for n in cfg['accounts'] if autopilot.worker_of_account(cfg, n) == worker]
    if not names:
        raise OpenAIError(f'no account runs the {worker} worker: add accounts.{worker} (its plan, window and reset) and '
                          f'workers.{worker}: {{kind: cli, command: [openai, -p, "{{prompt}}"]}} to the config')
    return names[0]


def verdict_of(st):
    """('start' or 'queue', why) from the account's pacing state."""
    if st.get('panic') or st.get('verdict') in NO_ROOM:
        return 'queue', f'{st["name"]} is {"in panic" if st.get("panic") else st["verdict"]}: queued until it has room'
    return 'start', f'{st["name"]} is {st.get("verdict") or "unknown"}: started now'


def account_state(cfg, name, now=None):
    now = now or datetime.now(timezone.utc)
    entries = pacing.read_log(state.usage_log_path(cfg))
    return pacing.account_state(name, cfg['accounts'][name], entries, now, *pacing.plan_args(cfg))


def start(cfg, repo, title, size='S', check='', notes=(), now=None, state_fn=None, dispatch_fn=None):
    """Adds the task (source openai, pinned to the openai worker) and starts it when the account has room."""
    if repo not in cfg['autopilot']['repos']:
        have = ', '.join(cfg['autopilot']['repos']) or 'none'
        raise OpenAIError(f'no repo "{repo}" under autopilot.repos: the repos are {have}')
    name = account_of(cfg)
    if not autopilot.step_of(cfg, name):
        raise OpenAIError(autopilot.unusable_why(cfg, name))
    st = (state_fn or account_state)(cfg, name)
    go, why = verdict_of(st)
    now = time.time() if now is None else now
    with backlog.locked(cfg) as data:
        t = backlog.new_task(repo, title, (), check or cfg['autopilot']['repos'][repo].get('check') or '', size, 'openai', notes)
        task, new = backlog.add(data, {**t, 'pin': f'worker:{WORKER}'})
        if not new and task['status'] != 'queued':
            return task['id'], task.get('run'), f'already in the backlog as {task["status"]}'
        if go != 'start':
            return task['id'], None, why
        run = (dispatch_fn or autopilot.dispatch)(cfg, task, name, now)
    return task['id'], run, why
