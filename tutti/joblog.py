"""The last lines of one job's log, for Chat's Terminal panel (t304, docs/CHAT.md).

Only a log the dashboard already lists for a job (the snapshot's jobs.items[].log) is read: a request names the job, never a
path. Credentials are replaced (scrub.secrets_only), the lines are bounded, and an admin path guards it (dashboard.ADMIN_PATHS)."""
import os
from urllib.parse import unquote

from . import scrub

API = '/api/job-log/'
MAX_LINES = 200
MAX_BYTES = 64 * 1024


def job_id(path):
    """The job id in `/api/job-log/<id>`, or ''."""
    return unquote(path[len(API):]) if path.startswith(API) else ''


def find(snapshot, jid):
    items = ((snapshot or {}).get('jobs') or {}).get('items') or []
    return next((j for j in items if str(j.get('id')) == jid), None)


def tail_text(data, lines=MAX_LINES):
    text = data.decode('utf8', 'replace')
    return '\n'.join(text.splitlines()[-lines:])


def tail(snapshot, jid, lines=MAX_LINES):
    """(status, answer): the job's last lines, or why there are none and what to do."""
    job = find(snapshot, jid)
    if not job:
        return 404, {'error': f'job-log: no job "{jid}" on the dashboard now: it finished or never ran here; open the Dashboard tab'}
    log = job.get('log')
    if not log or not os.path.isfile(log):
        return 404, {'error': f'job-log: job "{jid}" has no log file on this host: it runs elsewhere or writes none; '
                              'its card shows the progress'}
    try:
        with open(log, 'rb') as f:
            f.seek(max(0, os.path.getsize(log) - MAX_BYTES))
            data = f.read()
    except OSError as e:
        return 500, {'error': f'job-log: cannot read the log of "{jid}": {e.strerror}; check the file\'s permissions'}
    return 200, {'id': jid, 'title': job.get('title') or jid, 'status': job.get('status'),
                 'text': scrub.secrets_only(tail_text(data, max(1, min(int(lines), MAX_LINES))))}
