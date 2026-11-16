"""Windows helpers Tutti installs by itself on a WSL machine's Windows side (over its plain ssh, no person needed):

- the presence helper: a hidden PowerShell loop started at logon in the person's own console session, writing
  `%LOCALAPPDATA%\\tutti\\idle.txt` (`<seconds since last input> <locked 0|1> <unix time>`) every 30 s. GetLastInputInfo only
  answers for the session that calls it, and `query user` reports IDLE TIME `none` for a console session no matter how long
  nobody touched it (w5090, 2026-11-16: always "someone is at its keyboard"), so both the ssh session and `query user` are wrong;
- LibreHardwareMonitor (winget, else choco), started minimised to the tray at logon by a scheduled task with highest
  privileges (created once from the elevated ssh session: no prompt), with its web server on 127.0.0.1:8085 only, whose
  data.json gives the CPU package temperature and power (0.9.x dropped the WMI namespace).

`ensure(machine)` is idempotent and runs at most once an hour per machine."""
import base64
import json
import re
import subprocess
import time

IDLE_PS1 = r'''Add-Type @"
using System; using System.Runtime.InteropServices;
public class TuttiIdle { [StructLayout(LayoutKind.Sequential)] public struct I { public uint s; public uint t; }
[DllImport("user32.dll")] static extern bool GetLastInputInfo(ref I i);
public static double Sec() { I i = new I(); i.s = 8; GetLastInputInfo(ref i); return unchecked((uint)Environment.TickCount - i.t) / 1000.0; } }
"@
$d = Join-Path $env:LOCALAPPDATA 'tutti'; New-Item -ItemType Directory -Force $d | Out-Null
while ($true) {
    $locked = [int][bool](Get-Process LogonUI -ErrorAction SilentlyContinue)
    "$([int][TuttiIdle]::Sec()) $locked $([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())" | Set-Content (Join-Path $d 'idle.txt')
    Start-Sleep 30
}
'''

LHM_CONFIG = ('<?xml version="1.0" encoding="utf-8"?><configuration><appSettings>'
              '<add key="startMinMenuItem" value="true" /><add key="minTrayMenuItem" value="true" />'
              '<add key="minCloseMenuItem" value="true" /><add key="runWebServerMenuItem" value="true" />'
              '<add key="listenerIp" value="127.0.0.1" /><add key="listenerPort" value="8085" /></appSettings></configuration>')

SETUP = r'''$ErrorActionPreference = 'Continue'; $ProgressPreference = 'SilentlyContinue'
$d = Join-Path $env:LOCALAPPDATA 'tutti'; New-Item -ItemType Directory -Force $d | Out-Null
$ps1 = Join-Path $d 'idle.ps1'
[IO.File]::WriteAllText($ps1, [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__IDLE__')))
$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$trig = New-ScheduledTaskTrigger -AtLogOn -User $me
$p = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$a = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps1`""
Register-ScheduledTask -TaskName 'Tutti presence' -Action $a -Trigger $trig -Principal $p -Settings $set -Force | Out-Null
if (-not (Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -like '*tutti\idle.ps1*' })) {
    Start-ScheduledTask -TaskName 'Tutti presence'; 'started presence' }
$exe = Get-ChildItem -Recurse -Filter LibreHardwareMonitor.exe -ErrorAction SilentlyContinue `
    (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'), "$env:ProgramFiles\LibreHardwareMonitor", "$env:ProgramData\chocolatey\lib" |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $exe) {
    winget install --id LibreHardwareMonitor.LibreHardwareMonitor -e --silent --source winget --disable-interactivity `
        --accept-package-agreements --accept-source-agreements | Out-Null
    if ($LASTEXITCODE -ne 0 -and (Get-Command choco -ErrorAction SilentlyContinue)) { choco install librehardwaremonitor -y --no-progress | Out-Null }
    $exe = Get-ChildItem -Recurse -Filter LibreHardwareMonitor.exe -ErrorAction SilentlyContinue `
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'), "$env:ProgramFiles\LibreHardwareMonitor", "$env:ProgramData\chocolatey\lib" |
        Select-Object -First 1 -ExpandProperty FullName
}
if ($exe) {
    $cfg = Join-Path (Split-Path $exe) 'LibreHardwareMonitor.config'
    if (-not ((Test-Path $cfg) -and (Get-Content -Raw $cfg) -match 'key="runWebServerMenuItem" value="true"')) {
        Get-Process LibreHardwareMonitor -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep 2
        [IO.File]::WriteAllText($cfg, '__LHMCFG__') }
    $p2 = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName 'Tutti sensors' -Action (New-ScheduledTaskAction -Execute $exe) -Trigger $trig -Principal $p2 -Settings $set -Force | Out-Null
    if (-not (Get-Process LibreHardwareMonitor -ErrorAction SilentlyContinue)) { Start-ScheduledTask -TaskName 'Tutti sensors'; 'started sensors' }
} else { 'no LibreHardwareMonitor: winget and choco failed' }
'''

READ_LHM = 'curl.exe -s --max-time 5 http://127.0.0.1:8085/data.json'
READ_IDLE = 'type "%LOCALAPPDATA%\\tutti\\idle.txt"'
EVERY = 3600
STALE = 120
_done = {}


def setup_script():
    """One line for `powershell -Command -` (which runs stdin line by line and breaks multi-line blocks)."""
    idle = base64.b64encode(IDLE_PS1.encode()).decode()
    body = SETUP.replace('__IDLE__', idle).replace('__LHMCFG__', LHM_CONFIG)
    return f"iex ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{base64.b64encode(body.encode()).decode()}')))\n"


def ensure(machine, run=None, clock=time.time):
    """Install and start the presence helper and LibreHardwareMonitor on the machine's Windows side; at most once an hour."""
    if clock() - _done.get(machine['name'], -EVERY) < EVERY:
        return None
    _done[machine['name']] = clock()
    from . import remote
    run = run or subprocess.run
    try:
        # on stdin: the encoded form is longer than cmd.exe's 8191-character line
        r = run([*remote.SSH, *remote.QUICK, machine['host'], 'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command -'],
                input=setup_script(), capture_output=True, text=True, timeout=600, check=False)
        return r.returncode
    except (OSError, subprocess.SubprocessError):
        return None


def parse_idle(text, now):
    """Seconds since the console person's last input from idle.txt; None when locked (away) or the file is stale/missing;
    False when nothing usable was read (fall back to `query user`)."""
    parts = (text or '').split()
    try:
        idle, locked, at = float(parts[0]), parts[1] == '1', float(parts[2])
    except (IndexError, ValueError):
        return False
    if now - at > STALE:
        return False
    return None if locked else idle


def _num(text):
    m = re.match(r'\s*(-?\d+(?:[.,]\d+)?)', text or '')
    return float(m.group(1).replace(',', '.')) if m else None


def parse_lhm(text):
    """(CPU degrees C or None, CPU package W or None) from LibreHardwareMonitor's data.json (values are locale text, `56,3 °C`)."""
    try:
        tree = json.loads(text or '')
    except ValueError:
        return None, None
    temps, watts, stack = [], [], [tree]
    while stack:
        n = stack.pop()
        stack.extend(n.get('Children') or [])
        sid, name = n.get('SensorId') or '', n.get('Text') or ''
        if not re.match(r'/(amd|intel)cpu/', sid):
            continue
        if n.get('Type') == 'Temperature' and re.search(r'Tctl|Package|Core Max', name):
            temps.append(_num(n.get('Value')))
        elif n.get('Type') == 'Power' and name == 'Package':
            watts.append(_num(n.get('Value')))
    temps, watts = [t for t in temps if t is not None], [w for w in watts if w is not None]
    return (max(temps) if temps else None), (sum(watts) if watts else None)
