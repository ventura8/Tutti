"""Read Codex subscription limits through Codex CLI's app-server, without reading its auth files."""
import json
import re
import subprocess
from datetime import datetime, timezone

TIMEOUT = 20
REQUEST = '\n'.join((
    json.dumps({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'tutti', 'version': '0.1.0'}}}),
    json.dumps({'method': 'initialized', 'params': {}}),
    json.dumps({'id': 2, 'method': 'account/rateLimits/read'}),
    '',
))


def response(text):
    """The result of request 2 in an app-server's newline-delimited response, ignoring notifications."""
    for line in text.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get('id') == 2:
            if message.get('error'):
                raise ValueError('Codex did not return its usage limits: ' + str(message['error'].get('message', 'request failed')))
            result = message.get('result')
            if not isinstance(result, dict) or not isinstance(result.get('rateLimits'), dict):
                raise ValueError('Codex returned no rateLimits object')
            return result['rateLimits']
    raise ValueError('Codex app-server closed without a rate limits response')


def command(cfg, account):
    """The configured Codex executable, retaining custom install paths."""
    worker = ((cfg.get('workers') or {}).get(account.get('worker') or 'codex') or {})
    command = worker.get('command') or ['codex']
    return [command[0], 'app-server', '--listen', 'stdio://']


def read(cfg, account, run=subprocess.run):
    """Run one short, read-only account/rateLimits/read request and return both windows."""
    argv = command(cfg, account)
    try:
        result = run(argv, input=REQUEST, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired as e:
        raise ValueError(f'{argv[0]} app-server did not return usage within {TIMEOUT} seconds') from e
    except OSError as e:
        raise ValueError(f'{argv[0]} app-server could not start ({e})') from e
    if result.returncode:
        detail = (result.stderr or '').strip().splitlines()
        detail = re.sub(r'(?i)(bearer\s+)[^\s]+', r'\1[redacted]', detail[-1][:160]) if detail else ''
        raise ValueError(f'{argv[0]} app-server exited {result.returncode}' + (f': {detail}' if detail else ''))
    return response(result.stdout or '')


def window(rates, account, spec):
    """(used percentage, UTC reset) for the Codex bucket that matches accounts.<name>.window."""
    wanted = spec.get('bucket')
    windows = [(name, rates.get(name)) for name in ('primary', 'secondary') if isinstance(rates.get(name), dict)]
    if wanted in ('primary', 'secondary'):
        chosen = next((value for name, value in windows if name == wanted), None)
    else:
        desired = {'5h': 300, '7d': 10080}.get(account.get('window'))
        chosen = (min(windows, key=lambda item: abs(int(item[1].get('windowDurationMins') or 0) - desired))[1]
                  if windows and desired else None)
        if chosen and desired and abs(int(chosen.get('windowDurationMins') or 0) - desired) > desired * 0.2:
            chosen = None
    if not chosen:
        bucket = wanted or account.get('window') or 'configured window'
        raise ValueError(f'Codex has no {bucket} usage window for accounts.codex.window: check the Codex plan and window')
    used = chosen.get('usedPercent')
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
        raise ValueError('Codex usage response has no valid usedPercent: refresh the Codex account')
    reset = chosen.get('resetsAt')
    if isinstance(reset, bool) or not isinstance(reset, (int, float)) or reset <= 0:
        raise ValueError('Codex usage window has no reset time: read Codex again after it reports a reset')
    return float(used), datetime.fromtimestamp(reset, timezone.utc)


def parse(rates, account, spec):
    return window(rates, account, spec)
