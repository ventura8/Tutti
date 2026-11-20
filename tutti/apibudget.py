"""Disabled local API spend reservation prerequisite; no network or Chat code calls this module.

The ledger covers only calls a trusted local caller reserves, not other API clients or provider billing lag. It keeps the
whole worst-case reservation after completion, uncertainty or process death; it never automatically refunds money.
"""
import contextlib
import json
import os
import secrets
import stat
import threading

try:
    import fcntl
except ImportError:
    fcntl = None

MAX_VALUE = 10**18
MAX_ENTRIES = 10000
MAX_STATE_BYTES = 8 * 1024 * 1024
_THREAD_LOCK = threading.Lock()


class BudgetError(ValueError):
    """The local reservation cannot safely proceed."""


def _whole(value, name, *, allow_zero=False):
    if not isinstance(value, int) or isinstance(value, bool) or not (0 if allow_zero else 1) <= value <= MAX_VALUE:
        raise BudgetError(f'{name} must be a bounded whole number: check the API budget configuration.')
    return value


def _policy(policy):
    if not isinstance(policy, dict) or set(policy) != {'model', 'limit_micros', 'input_micros_per_million', 'output_micros_per_million'}:
        raise BudgetError('API budget policy is incomplete: supply model, limit and both token prices.')
    if not isinstance(policy['model'], str) or not policy['model'].strip() or len(policy['model']) > 200:
        raise BudgetError('API budget model is invalid: choose one configured model.')
    for key in ('limit_micros', 'input_micros_per_million', 'output_micros_per_million'):
        _whole(policy[key], key)
    return dict(policy)


def ceiling(policy, input_tokens, output_tokens):
    """Conservative fixed-rate maximum in microdollars, rounded up per direction."""
    policy = _policy(policy)
    inp = _whole(input_tokens, 'input_tokens', allow_zero=True)
    out = _whole(output_tokens, 'output_tokens')
    amount = ((inp * policy['input_micros_per_million'] + 999999) // 1000000 +
              (out * policy['output_micros_per_million'] + 999999) // 1000000)
    return _whole(amount, 'reservation')


def _open_private_dir(folder):
    path = os.path.abspath(folder)
    current = os.path.sep
    for part in path.strip(os.path.sep).split(os.path.sep):
        if not part:
            continue
        current = os.path.join(current, part)
        st = os.lstat(current)
        trusted_sticky = st.st_uid == 0 and bool(st.st_mode & stat.S_ISVTX)
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid not in (0, os.geteuid())
                or (st.st_mode & 0o022 and not trusted_sticky)):
            raise BudgetError('API budget path has an untrusted or linked ancestor: use a private state folder.')
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    st = os.fstat(fd)
    if st.st_uid != os.geteuid() or st.st_mode & 0o077:
        os.close(fd)
        raise BudgetError('API budget folder is not private to this user: fix its owner and permissions.')
    return fd


@contextlib.contextmanager
def _locked(folder):
    if fcntl is None or not getattr(os, 'O_NOFOLLOW', 0) or not getattr(os, 'O_DIRECTORY', 0):
        raise BudgetError('API budget locking is unavailable on this platform: leave paid routing disabled.')
    with _THREAD_LOCK:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        directory = _open_private_dir(folder)
        try:
            try:
                fd = os.open('.budget.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            except OSError as e:
                raise BudgetError('API budget lock cannot be opened: check the private state folder.') from e
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
                    raise BudgetError('API budget lock is not a private regular file: repair the state folder.')
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                current = os.stat('.budget.lock', dir_fd=directory, follow_symlinks=False)
                if (st.st_dev, st.st_ino) != (current.st_dev, current.st_ino):
                    raise BudgetError('API budget lock changed during acquisition: do not send a paid request.')
                yield directory
            finally:
                os.close(fd)
        finally:
            os.close(directory)


def _check_entry(key, entry):
    if not isinstance(key, str) or not isinstance(entry, dict):
        raise BudgetError('API budget entry is malformed: do not send a paid request.')
    reserved = _whole(entry.get('reserved_micros'), 'reserved_micros')
    liability = _whole(entry.get('liability_micros'), 'liability_micros')
    result = entry.get('result_micros')
    if result is not None:
        _whole(result, 'result_micros', allow_zero=True)
    if liability < reserved or (result is not None and liability < result):
        raise BudgetError('API budget entry understates its liability: do not send a paid request.')


def _unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate key')
        out[key] = value
    return out


def _load(directory):
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        fd = os.open('budget.json', flags, dir_fd=directory)
    except OSError as e:
        raise BudgetError('API budget state is missing or unreadable: do not send a paid request.') from e
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX_STATE_BYTES
                or st.st_uid != os.geteuid() or st.st_mode & 0o077):
            raise BudgetError('API budget state is invalid: do not send a paid request.')
        with os.fdopen(fd, 'r', encoding='utf8') as f:
            fd = -1
            data = json.load(f, object_pairs_hook=_unique_pairs)
    except (OSError, ValueError) as e:
        raise BudgetError('API budget state cannot be read: do not send a paid request.') from e
    finally:
        if fd != -1:
            os.close(fd)
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('entries'), dict):
        raise BudgetError('API budget state is malformed: do not send a paid request.')
    _policy(data.get('policy'))
    if len(data['entries']) > MAX_ENTRIES:
        raise BudgetError('API budget state is full: reconcile it before requesting paid work.')
    for key, entry in data['entries'].items():
        _check_entry(key, entry)
    return data


def _save(directory, data):
    tmp = '.budget-' + secrets.token_hex(16)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as f:
            json.dump(data, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, 'budget.json', src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(tmp, dir_fd=directory)
        except FileNotFoundError:
            pass


def initialize(folder, policy):
    """Explicitly create a ledger once. This must never be called automatically after missing state."""
    policy = _policy(policy)
    with _locked(folder) as directory:
        try:
            os.stat('budget.json', dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            _save(directory, {'version': 1, 'policy': policy, 'entries': {}})
        else:
            raise BudgetError('API budget state already exists: preserve and reconcile it instead of resetting it.')


def reserve(folder, policy, request_id, input_tokens, output_tokens):
    """Durably reserve a worst-case amount once, before a paid request; no network happens here."""
    policy, amount = _policy(policy), ceiling(policy, input_tokens, output_tokens)
    if not isinstance(request_id, str) or len(request_id) != 32 or any(c not in '0123456789abcdef' for c in request_id):
        raise BudgetError('API reservation ID is invalid: use a unique 32-character lowercase hexadecimal ID.')
    with _locked(folder) as directory:
        data = _load(directory)
        if data['policy'] != policy:
            raise BudgetError('API budget policy changed: reconcile the existing ledger before making paid requests.')
        entries = data['entries']
        if request_id in entries:
            raise BudgetError('API reservation ID was already used: do not retry an ambiguous request.')
        if len(entries) >= MAX_ENTRIES:
            raise BudgetError('API budget ledger is full: reconcile it before requesting paid work.')
        if sum(e['liability_micros'] for e in entries.values()) + amount > policy['limit_micros']:
            raise BudgetError('API budget has insufficient local room: do not send the request.')
        entries[request_id] = {'reserved_micros': amount, 'liability_micros': amount, 'result_micros': None}
        _save(directory, data)
    return amount


def record_result(folder, request_id, actual_micros):
    """Record a known outcome; never reduce liability, even if the reported cost is lower."""
    actual = _whole(actual_micros, 'actual_micros', allow_zero=True)
    with _locked(folder) as directory:
        data = _load(directory)
        entry = data['entries'].get(request_id)
        if entry is None:
            raise BudgetError('API reservation is missing: reconcile the ledger before recording a result.')
        previous = entry.get('result_micros')
        if previous is not None and previous != actual:
            raise BudgetError('API reservation already has a different result: reconcile it before retrying.')
        entry['result_micros'] = actual
        entry['liability_micros'] = max(entry['liability_micros'], actual)
        _save(directory, data)
