"""Conservative Docker inspection classification for future run recovery (t309).

This module accepts data only. It does not contact a daemon, create or stop containers, authenticate a response, or
permit a task retry. Even a stopped candidate needs a separately authenticated manager, durable launch ownership and
proof that no external effects escaped the container before it can be used for recovery.
"""
import re
from datetime import datetime
from pathlib import PurePosixPath

RUN_ID = re.compile(r'^[0-9a-f]{32}$')
CONTAINER_ID = re.compile(r'^[0-9a-f]{64}$')
BOOT_ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
ENGINE_ID = re.compile(r'^[A-Za-z0-9:._-]{1,128}$')
USER = re.compile(r'^([1-9][0-9]*):([1-9][0-9]*)$')
LABEL = 'io.tutti.run'
DOCKER_TIME = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$')


def workspace_path(path):
    """A lexical candidate only: callers must independently verify and pin the actual mount identity."""
    if not isinstance(path, str) or not path.startswith('/') or '\x00' in path:
        return False
    parts = path.split('/')
    return (path != '/' and all(part not in ('.', '..', '') for part in parts[1:])
            and str(PurePosixPath(path)) == path)


def identity_matches(expected, snapshot):
    """The IDs must be explicit, complete and identical; missing manager/container state remains unknown."""
    if not isinstance(expected, dict) or not isinstance(snapshot, dict):
        return False
    run, cid, boot, engine = (expected.get(k) for k in ('run', 'container', 'boot', 'engine'))
    if not all(isinstance(v, str) for v in (run, cid, boot, engine)):
        return False
    if not (RUN_ID.fullmatch(run) and CONTAINER_ID.fullmatch(cid) and BOOT_ID.fullmatch(boot) and ENGINE_ID.fullmatch(engine)):
        return False
    if snapshot.get('boot') != boot or snapshot.get('engine') != engine:
        return False
    inspected = snapshot.get('inspect')
    if not isinstance(inspected, dict) or inspected.get('Id') != cid:
        return False
    config = inspected.get('Config')
    labels = config.get('Labels') if isinstance(config, dict) else None
    return isinstance(labels, dict) and labels.get(LABEL) == run


def restricted_host(host):
    if host.get('NetworkMode') != 'none' or host.get('Privileged') is not False or host.get('AutoRemove') is not False:
        return False
    if host.get('PidMode') not in ('', 'private') or host.get('IpcMode') != 'private' or host.get('CgroupnsMode') != 'private':
        return False
    if host.get('UsernsMode') not in ('', 'private') or host.get('ReadonlyRootfs') is not True:
        return False
    if any(k not in host or host[k] not in (None, []) for k in ('CapAdd', 'Devices', 'DeviceRequests', 'VolumesFrom')):
        return False
    if host.get('CapDrop') != ['ALL'] or host.get('Tmpfs') not in (None, {}):
        return False
    options, restart = host.get('SecurityOpt'), host.get('RestartPolicy')
    return (isinstance(options, list) and 'no-new-privileges:true' in options
            and all(isinstance(x, str) and 'unconfined' not in x.lower() for x in options)
            and all(not x.lower().startswith('no-new-privileges') or x == 'no-new-privileges:true' for x in options)
            and isinstance(restart, dict) and restart.get('Name') == 'no')


def single_workspace(mounts, workspace):
    if not workspace_path(workspace) or not isinstance(mounts, list) or len(mounts) != 1 or not isinstance(mounts[0], dict):
        return False
    mount = mounts[0]
    return (mount.get('Type') == 'bind' and mount.get('Source') == workspace
            and mount.get('Destination') == workspace and mount.get('RW') is True)


def isolated_candidate(expected, inspected):
    """Require a narrow local/no-network configuration; this is a necessary condition, not a sandbox proof."""
    host, cfg = inspected.get('HostConfig'), inspected.get('Config')
    return (isinstance(host, dict) and isinstance(cfg, dict) and isinstance(cfg.get('User'), str)
            and bool(USER.fullmatch(cfg['User'])) and restricted_host(host)
            and single_workspace(inspected.get('Mounts'), expected.get('workspace')))


def finished_at(value):
    match = DOCKER_TIME.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return False
    base, fraction, zone = match.groups()
    normalized = base + ('.' + fraction[:6].ljust(6, '0') if fraction else '') + ('+00:00' if zone == 'Z' else zone)
    try:
        dt = datetime.fromisoformat(normalized)
    except (TypeError, ValueError, OverflowError):
        return False
    return dt.year >= 1970 and dt.tzinfo is not None


def classify(expected, snapshot):
    """Return observed-running, stopped-candidate or unknown. Never authorizes retries, kills or lease release."""
    if not identity_matches(expected, snapshot):
        return 'unknown'
    inspected = snapshot['inspect']
    state = inspected.get('State')
    if not isinstance(state, dict):
        return 'unknown'
    if (state.get('Running') is True and state.get('Status') == 'running' and state.get('Restarting') is False
            and state.get('Paused') is False and state.get('Dead') is False):
        return 'observed-running'
    if not isolated_candidate(expected, inspected):
        return 'unknown'
    if (state.get('Status') != 'exited' or state.get('Running') is not False or state.get('Restarting') is not False
            or state.get('Paused') is not False or state.get('Dead') is not False or type(state.get('Pid')) is not int or state['Pid'] != 0
            or type(state.get('ExitCode')) is not int or not finished_at(state.get('FinishedAt'))):
        return 'unknown'
    return 'stopped-candidate'
