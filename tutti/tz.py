"""Time zones: which IANA zone a computer is in, found on its own, and how a time is shown to a person.

Every stored time is UTC (ISO 8601 with Z, or epoch seconds); a time is turned into local time only at the edges: a
profile bucket (a local weekday x hour, so a daylight-saving change never shifts what was learned), owner hours, quiet
hours, tariffs, vacation dates, and what the CLI prints. `time.zone: auto` (the default) detects the host's zone (TZ,
/etc/timezone, /etc/localtime, timedatectl, Windows `tzutil /g`, macOS `systemsetup -gettimezone`); an IANA name overrides it.
A remote machine's zone is read over its ssh (`remote_command`, `parse_remote`)."""
import functools
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Windows zone names (tzutil /g) to IANA: CLDR's windowsZones.xml, territory 001, the common ones
WINDOWS = {
    'Dateline Standard Time': 'Etc/GMT+12', 'Hawaiian Standard Time': 'Pacific/Honolulu', 'Alaskan Standard Time': 'America/Anchorage',
    'Pacific Standard Time': 'America/Los_Angeles', 'US Mountain Standard Time': 'America/Phoenix',
    'Mountain Standard Time': 'America/Denver', 'Central Standard Time': 'America/Chicago',
    'Central America Standard Time': 'America/Guatemala', 'Canada Central Standard Time': 'America/Regina',
    'Central Standard Time (Mexico)': 'America/Mexico_City', 'Eastern Standard Time': 'America/New_York',
    'SA Pacific Standard Time': 'America/Bogota', 'Atlantic Standard Time': 'America/Halifax',
    'Venezuela Standard Time': 'America/Caracas', 'SA Western Standard Time': 'America/La_Paz',
    'Newfoundland Standard Time': 'America/St_Johns', 'E. South America Standard Time': 'America/Sao_Paulo',
    'Argentina Standard Time': 'America/Buenos_Aires', 'Greenland Standard Time': 'America/Godthab', 'UTC': 'Etc/UTC',
    'Azores Standard Time': 'Atlantic/Azores', 'GMT Standard Time': 'Europe/London', 'Greenwich Standard Time': 'Atlantic/Reykjavik',
    'Morocco Standard Time': 'Africa/Casablanca', 'W. Europe Standard Time': 'Europe/Berlin',
    'Central Europe Standard Time': 'Europe/Budapest', 'Romance Standard Time': 'Europe/Paris',
    'Central European Standard Time': 'Europe/Warsaw', 'W. Central Africa Standard Time': 'Africa/Lagos',
    'GTB Standard Time': 'Europe/Bucharest', 'E. Europe Standard Time': 'Europe/Chisinau', 'FLE Standard Time': 'Europe/Kiev',
    'Egypt Standard Time': 'Africa/Cairo', 'South Africa Standard Time': 'Africa/Johannesburg', 'Israel Standard Time': 'Asia/Jerusalem',
    'Turkey Standard Time': 'Europe/Istanbul', 'Arabic Standard Time': 'Asia/Baghdad', 'Arab Standard Time': 'Asia/Riyadh',
    'Russian Standard Time': 'Europe/Moscow', 'E. Africa Standard Time': 'Africa/Nairobi', 'Iran Standard Time': 'Asia/Tehran',
    'Arabian Standard Time': 'Asia/Dubai', 'Pakistan Standard Time': 'Asia/Karachi', 'India Standard Time': 'Asia/Calcutta',
    'Nepal Standard Time': 'Asia/Katmandu', 'Bangladesh Standard Time': 'Asia/Dhaka', 'SE Asia Standard Time': 'Asia/Bangkok',
    'China Standard Time': 'Asia/Shanghai', 'Singapore Standard Time': 'Asia/Singapore', 'Taipei Standard Time': 'Asia/Taipei',
    'W. Australia Standard Time': 'Australia/Perth', 'Tokyo Standard Time': 'Asia/Tokyo', 'Korea Standard Time': 'Asia/Seoul',
    'Cen. Australia Standard Time': 'Australia/Adelaide', 'AUS Eastern Standard Time': 'Australia/Sydney',
    'E. Australia Standard Time': 'Australia/Brisbane', 'New Zealand Standard Time': 'Pacific/Auckland',
}

# one shell line for a remote Linux or macOS machine over ssh: its zone name, or the /etc/localtime link
REMOTE = ('cat /etc/timezone 2>/dev/null || readlink /etc/localtime 2>/dev/null || '
          'timedatectl show -p Timezone --value 2>/dev/null || echo "$TZ"')


def _name(text):
    """An IANA name out of a file's or a command's text: /etc/timezone's line, a zoneinfo path, `Time Zone: X`."""
    text = (text or '').strip().splitlines()[0].strip() if (text or '').strip() else ''
    if 'zoneinfo/' in text:
        text = text.split('zoneinfo/', 1)[1]
    if ':' in text and not text.startswith(':'):   # macOS `Time Zone: Europe/Bucharest`
        text = text.split(':', 1)[1].strip()
    text = text.lstrip(':').strip()
    if text in WINDOWS:
        return WINDOWS[text]
    return text if valid(text) else ''


def valid(name):
    """Whether `name` is an IANA zone this Python knows."""
    if not name or not isinstance(name, str):
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _read(path):
    try:
        with open(path, encoding='utf8') as f:
            return f.read()
    except OSError:
        return ''


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def detect(env=None, read=_read, run=_run, link=os.path.realpath, system=None):
    """(IANA zone name, how it was found) of this computer; ('', '') when nothing says (UTC is then used). Never asks."""
    env = os.environ if env is None else env
    system = system or os.name
    steps = [('TZ', lambda: env.get('TZ', ''))]
    if system == 'nt':
        steps.append(('tzutil /g', lambda: run(['tzutil', '/g'])))
    else:
        steps += [('/etc/timezone', lambda: read('/etc/timezone')),
                  ('/etc/localtime', lambda: link('/etc/localtime')),
                  ('timedatectl', lambda: run(['timedatectl', 'show', '-p', 'Timezone', '--value'])),
                  ('systemsetup', lambda: run(['systemsetup', '-gettimezone']))]
    for how, get in steps:
        name = _name(get())
        if name:
            return name, how
    return '', ''


@functools.lru_cache(maxsize=1)
def host_zone():
    return detect()[0]


_CHOSEN = {'zone': 'auto'}   # time.zone of the loaded config (config.load sets it), so pure code needs no cfg argument


def use(cfg):
    _CHOSEN['zone'] = ((cfg or {}).get('time') or {}).get('zone') or 'auto'


def errors(cfg):
    want = (cfg.get('time') or {}).get('zone') or 'auto'
    if want == 'auto' or valid(want):
        return []
    return [f'time.zone is {want!r}: auto (this computer\'s, detected) or an IANA name such as Europe/Bucharest']


def zone_name(cfg=None, browser=''):
    """The zone this Tutti means by local time: `time.zone` when it is an IANA name, else the detected one, else the zone
    the owner's browser reported to the dashboard (`browser`), else UTC."""
    want = ((cfg or {}).get('time') or {}).get('zone') or _CHOSEN['zone']
    if want != 'auto' and valid(want):
        return want
    return host_zone() or (browser if valid(browser) else '') or 'UTC'


def zone(cfg_or_name=None):
    """A tzinfo: from a name, a config, a number of hours (an old fixed offset) or a tzinfo as is."""
    if isinstance(cfg_or_name, tzinfo):
        return cfg_or_name
    if isinstance(cfg_or_name, (int, float)) and not isinstance(cfg_or_name, bool):
        return timezone(timedelta(hours=cfg_or_name))
    name = cfg_or_name if isinstance(cfg_or_name, str) else zone_name(cfg_or_name)
    return ZoneInfo(name) if valid(name) else timezone.utc


def local(at, z):
    """`at` (aware; a naive one is UTC) in zone `z`."""
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return at.astimezone(zone(z))


def localtime(ts=None, z=None):
    """time.localtime's struct_time for epoch seconds `ts` (now by default), in zone `z` (time.zone by default)."""
    at = datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc)
    return local(at, z if z is not None else zone()).timetuple()


def local_now(ts=None, z=None):
    """A naive local datetime for epoch seconds `ts` (now by default) in zone `z` (time.zone by default)."""
    at = datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc)
    return local(at, z if z is not None else zone()).replace(tzinfo=None)


def to_utc(naive_local, z):
    """A naive local wall time in zone `z` as an aware UTC datetime (a gap or a repeat takes the first reading)."""
    return naive_local.replace(tzinfo=zone(z)).astimezone(timezone.utc)


def offset_hours(z, at=None):
    """The zone's UTC offset in hours at `at` (now by default): 2 for Bucharest in winter, 3 in summer."""
    return local(at or datetime.now(timezone.utc), z).utcoffset().total_seconds() / 3600


def label(hours):
    """'UTC+2', 'UTC-3:30', 'UTC'."""
    if not hours:
        return 'UTC'
    sign, mins = '+' if hours > 0 else '-', round(abs(hours) * 60)
    return f'UTC{sign}{mins // 60}' + (f':{mins % 60:02d}' if mins % 60 else '')


def show(at, z=None, now=None, date=True):
    """A time for a person, in local time with its offset: 'Sun 15 Nov 02:00 (UTC+2)'; within a day, '21:00 (UTC+2, in 3.5 h)'."""
    lt = local(at, z if z is not None else zone())
    off = label(lt.utcoffset().total_seconds() / 3600)
    if now is not None and not date:
        hours = max(0.0, (at - now).total_seconds() / 3600)
        if hours < 24:
            return f'{lt:%H:%M} ({off}, in {hours:.1f} h)'
    return f'{lt:%a %d %b %H:%M} ({off})'


def remote_command():
    return REMOTE


def parse_remote(text):
    """The zone name a remote machine printed for REMOTE ('' when none)."""
    return _name(text)
