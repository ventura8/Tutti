"""Windows UI runs in the logged-on user's interactive desktop (docs/DISPATCH.md, *Windows desktop runs*). A job that
reaches the PC over ssh runs in session 0, where UI automation (Appium, WinAppDriver) finds no input desktop. A suite
with `desktop: {user, dir, before}` has its Windows command run as a one-time scheduled task with `/ru <user> /it`: it
runs inside that user's console session while they are logged on (no password asked or stored). The wrapper writes the
output to a log Tutti follows and the exit code to a file it waits for; the task and its folder are removed however the
run ends. Pure: shell generation only. `user` is validated (docs/SECURITY-AUDIT-POST-QUEUE.md, P1): it lands in a bash path."""
import re
import shlex

POLL = 5
# a Windows SAM account name: never shell metacharacters (it is concatenated into a WSL path before schtasks).
# \Z (not $): Python's $ also matches before a trailing newline, which would still break the bash path.
USER_RX = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')   # never starts with "-" (schtasks /ru)
# a cmd.exe working directory: no new lines or shell metacharacters that would break out of `cd /d`
DIR_RX = re.compile(r'^[A-Za-z]:\\[^&\r\n|<>"]{0,240}\Z')


def wrap(command, user, cwd='', before=''):
    """The bash line (run in the PC's WSL, with Windows interop) that runs `command` (a cmd.exe line) in `user`'s
    interactive session and exits with its exit code. `before` runs first in bash (a yield-flag check);
    what it puts in $tutti_after runs at the end. Raises ValueError when `user` or `cwd` would break the shell."""
    why = user_refusal(user) or (dir_refusal(cwd) if cwd else '')
    if why:
        raise ValueError(why)
    q = shlex.quote
    name = 'tutti-ui-$$'
    d = f'/mnt/c/Users/{user}/AppData/Local/Temp/{name}'
    cd = f'cd /d {cwd}\r\n' if cwd else ''
    body = f'@echo off\r\n{cd}{command} > "%~dp0out.log" 2>&1\r\necho %ERRORLEVEL% > "%~dp0rc.txt"\r\n'
    # `before` may leave its own clean-up in $tutti_after (a trap of its own would be replaced by this one)
    clean = f'schtasks.exe /delete /f /tn {name} > /dev/null 2>&1; rm -rf {d}; eval "${{tutti_after:-:}}"'
    return (f'{before + "; " if before else ""}d={d}; mkdir -p "$d" && printf %s {q(body)} > "$d/run.cmd" && '
            f'trap {q(clean)} EXIT && '
            f'schtasks.exe /create /f /tn {name} /ru {q(user)} /it /sc once /st 00:00 /tr "$(wslpath -w "$d")\\run.cmd" > /dev/null && '
            f'schtasks.exe /run /tn {name} > /dev/null && echo "tutti: running in {user}\'s desktop session" && '
            # the log grows on the Windows drive, where tail -F sees no change: new bytes are read by size instead
            f'{{ n=0; show() {{ s=$(stat -c %s "$d/out.log" 2>/dev/null || echo 0); [ "$s" -gt "$n" ] && '
            f'tail -c +$((n + 1)) "$d/out.log" | head -c $((s - n)); n=$s; }}; '
            f'until [ -s "$d/rc.txt" ]; do show; sleep {POLL}; done; sleep 1; show; '
            f'exit "$(tr -dc 0-9 < "$d/rc.txt")"; }}')


def user_refusal(user):
    if not isinstance(user, str) or not USER_RX.match(user):
        return (f'desktop.user is {user!r}: use a Windows login name (letters, digits, ".", "_" or "-", at most 64), '
                'never a shell or path character')
    return ''


def dir_refusal(cwd):
    if not isinstance(cwd, str) or not DIR_RX.match(cwd):
        return (f'desktop.dir is {cwd!r}: use a Windows path like C:\\work\\tree (drive letter, no &, |, <, > or quotes)')
    return ''


def errors(key, d):
    if d is None:
        return []
    if not isinstance(d, dict) or not isinstance(d.get('user'), str) or not d['user'].strip():
        return [f'{key} is {d!r}: write it as {{user: <the logged-on Windows user>, dir: <C:\\\\work\\\\tree>, before: <bash line>}}']
    out = []
    # validate the raw value (no .strip): a trailing newline must fail, same as wrap()
    if why := user_refusal(d['user']):
        out.append(f'{key}.user: {why}')
    if d.get('dir') and (why := dir_refusal(d['dir'])):
        out.append(f'{key}.dir: {why}')
    return out
