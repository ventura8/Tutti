"""Read Codex subscription limits through Codex CLI's app-server, without reading its auth files."""
import json
import math
import queue
import subprocess
import threading
import time
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


def exchange(process, lines, executable):
    """Initialize before requesting limits, while allowing notifications between responses."""
    requests = REQUEST.splitlines()
    process.stdin.write(requests[0] + '\n')
    process.stdin.flush()
    deadline, initialized = time.monotonic() + TIMEOUT, False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError(f'{executable} app-server did not return usage within {TIMEOUT} seconds')
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty as e:
            raise ValueError(f'{executable} app-server did not return usage within {TIMEOUT} seconds') from e
        if line is None:
            raise ValueError('Codex app-server closed without a rate limits response')
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get('id') == 1 and not initialized:
            if message.get('error'):
                raise ValueError('Codex app-server initialization failed: check the Codex installation')
            initialized = True
            process.stdin.write('\n'.join(requests[1:]) + '\n')
            process.stdin.flush()
        elif message.get('id') == 2:
            return response(line)


def read(cfg, account, popen=subprocess.Popen):
    """Run one short, read-only account/rateLimits/read request and return both windows."""
    argv = command(cfg, account)
    try:
        process = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError as e:
        raise ValueError(f'{argv[0]} app-server could not start ({e})') from e
    lines = queue.Queue()

    def collect():
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=collect, daemon=True).start()
    try:
        return exchange(process, lines, argv[0])
    except OSError as e:
        raise ValueError(f'{argv[0]} app-server communication failed ({e})') from e
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()


def window(rates, account, spec):
    """(used percentage, UTC reset) for the Codex bucket that matches accounts.<name>.window."""
    wanted = spec.get('bucket')
    windows = [(name, rates.get(name)) for name in ('primary', 'secondary') if isinstance(rates.get(name), dict)]
    if wanted in ('primary', 'secondary'):
        chosen = next((value for name, value in windows if name == wanted), None)
    else:
        desired = {'5h': 300, '7d': 10080}.get(account.get('window'))
        windows = [(value, duration_minutes(value.get('windowDurationMins'))) for _, value in windows]
        windows = [(value, duration) for value, duration in windows if duration is not None]
        chosen, duration = min(windows, key=lambda item: abs(item[1] - desired)) if windows and desired else (None, None)
        if chosen and desired and abs(duration - desired) > desired * 0.2:
            chosen = None
    if not chosen:
        bucket = wanted or account.get('window') or 'configured window'
        raise ValueError(f'Codex has no {bucket} usage window for accounts.codex.window: check the Codex plan and window')
    used = chosen.get('usedPercent')
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
        raise ValueError('Codex usage response has no valid usedPercent: refresh the Codex account')
    reset = chosen.get('resetsAt')
    if not valid_duration(reset):
        raise ValueError('Codex usage window has no reset time: read Codex again after it reports a reset')
    try:
        return float(used), datetime.fromtimestamp(reset, timezone.utc)
    except (OverflowError, OSError, ValueError) as e:
        raise ValueError('Codex usage window has an invalid reset time: refresh the Codex account') from e


def valid_duration(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def duration_minutes(value):
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            return None
    return value if valid_duration(value) else None


def parse(rates, account, spec):
    return window(rates, account, spec)
