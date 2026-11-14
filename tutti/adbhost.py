"""adb on one machine, over the machine's transport (tutti/remote.py): run adb, find it, and handle the machine's adb key
pair (~/.android/adbkey; for a Windows adb.exe reached from WSL, %USERPROFILE%\\.android). A key never travels in argv or
a log: it is read into memory and written through standard input, at mode 600."""
import base64
import shlex
import subprocess
import time

from . import adbpair

USER_TOOLS = '$HOME/.local/share/tutti/platform-tools'
PLATFORM_TOOLS_URL = 'https://dl.google.com/android/repository/platform-tools-latest-linux.zip'

# Finds adb: the configured one, then PATH, the Android SDK, Tutti's own user-space copy, a Windows SDK seen from WSL.
FIND = ('A=${ADB:-}; if [ -z "$A" ]; then for c in "$(command -v adb 2>/dev/null)" "$HOME/Android/Sdk/platform-tools/adb" '
        f'"{USER_TOOLS}/adb" /mnt/c/Users/*/AppData/Local/Android/Sdk/platform-tools/adb.exe; do '
        '[ -n "$c" ] && [ -x "$c" ] && { A=$c; break; }; done; fi; ')
# The folder of the key that adb uses: a Windows adb.exe keeps its own under %USERPROFILE%.
KEYDIR = ('case "$A" in *.exe) K="$(wslpath "$(cmd.exe /c echo %USERPROFILE% 2>/dev/null | tr -d \'\\r\')")/.android";; '
          '*) K="${ANDROID_USER_HOME:-$HOME/.android}";; esac; ')


class Host:
    """One machine's adb. `machine` is a normalised config entry; `adb` a path that overrides the search."""

    def __init__(self, name, machine, adb=None, key_dir=None, sh=None):
        from . import remote  # remote imports routing, which imports devices
        self.name, self.machine, self.key_dir = name, machine, key_dir
        self.env = f'ADB={shlex.quote(adb)}; ' if adb else ''
        self._sh = sh or remote.sh

    def bash(self, body, stdin=None, timeout=120, stdout_only=False):
        """(exit code, output) of a bash line on the machine, after adb is found ($A) and its key folder ($K)."""
        keydir = f'K={shlex.quote(self.key_dir)}; ' if self.key_dir else KEYDIR
        script = f'{self.env}{FIND}{keydir}{body}'
        try:
            r = self._sh(self.machine, script, capture=True, timeout=timeout, stdin=stdin)
        except (OSError, subprocess.SubprocessError) as e:
            return 255, f'{self.name} did not answer: {e}'
        return r.returncode, (r.stdout or '') + ('' if stdout_only else (r.stderr or ''))

    def adb(self, *args, timeout=60):
        return self.bash(f'[ -n "$A" ] || exit 127; "$A" {shlex.join(args)} 2>&1 | tr -d \'\\r\'', timeout=timeout)

    def path(self):
        rc, out = self.bash('[ -n "$A" ] && echo "$A"')
        return out.strip() if rc == 0 else ''

    def version(self):
        """The platform-tools major version (0: adb missing or not running)."""
        return adbpair.parse_version(self.adb('version', timeout=30)[1])

    def state(self, serial):
        rc, out = self.adb('-s', serial, 'get-state', timeout=20)
        return out.strip().splitlines()[-1] if rc == 0 and out.strip() else 'missing'

    def listen(self):
        """The devices' connect services heard over mDNS with avahi-browse, touching no device and starting no adb server
        (an adb server auto-connects to what it hears); None when avahi-browse is not there."""
        rc, out = self.bash('command -v avahi-browse > /dev/null || exit 3; timeout 10 avahi-browse -t -r -p _adb-tls-connect._tcp',
                            timeout=30, stdout_only=True)
        return None if rc == 3 else adbpair.parse_avahi(out)

    def services(self, tries=6):
        """The mDNS services adb sees. A server that just started has not heard the devices yet: ask again for ~10 s."""
        body = ('[ -n "$A" ] || exit 127; "$A" start-server > /dev/null 2>&1; '
                f'for i in $(seq {int(tries)}); do o=$("$A" mdns services 2>&1 | tr -d \'\\r\'); '
                'printf %s "$o" | grep -q _adb-tls-connect && break; sleep 2; done; printf \'%s\\n\' "$o"')
        return adbpair.parse_services(self.bash(body, timeout=60)[1])

    def fingerprint(self):
        """A short hash of the public key (never the key): '' when the machine has no adb key yet."""
        rc, out = self.bash('[ -s "$K/adbkey.pub" ] && cut -d" " -f1 "$K/adbkey.pub" | sha256sum | cut -c1-16')
        return out.strip() if rc == 0 else ''

    def read_key(self):
        """(private, public) key text, kept in memory only; ('', '') when unreadable."""
        rc, priv = self.bash('cat "$K/adbkey"', stdout_only=True)   # never stderr: an ssh banner would corrupt the key
        rc2, pub = self.bash('cat "$K/adbkey.pub"', stdout_only=True)
        return (priv, pub) if rc == 0 and rc2 == 0 and priv.strip() and pub.strip() else ('', '')

    def write_key(self, priv, pub, stamp):
        """Installs the key pair at mode 600 (the public half 644), first moving a different existing pair to
        adbkey.tutti-backup-<stamp>. Returns (ok, the backup's suffix or '')."""
        suffix = f'tutti-backup-{stamp}'
        body = ('umask 077; mkdir -p "$K" && b=""; if [ -e "$K/adbkey" ]; then '
                f'cp -p "$K/adbkey" "$K/adbkey.{suffix}" && cp -p "$K/adbkey.pub" "$K/adbkey.pub.{suffix}" 2>/dev/null; b=1; fi; '
                'IFS= read -r a && IFS= read -r p && printf %s "$a" | base64 -d > "$K/adbkey.tutti-new" && '
                'printf %s "$p" | base64 -d > "$K/adbkey.pub.tutti-new" && '
                'chmod 600 "$K/adbkey.tutti-new" && chmod 644 "$K/adbkey.pub.tutti-new" && '
                'mv "$K/adbkey.tutti-new" "$K/adbkey" && mv "$K/adbkey.pub.tutti-new" "$K/adbkey.pub" && echo "ok${b:+ backup}"')
        packed = '\n'.join(base64.b64encode(t.encode()).decode() for t in (priv, pub)) + '\n'
        rc, out = self.bash(body, stdin=packed, timeout=60)
        ok = rc == 0 and out.strip().startswith('ok')
        return ok, suffix if ok and 'backup' in out else ''

    def restore_key(self, suffix):
        """Puts the backed-up key pair back (a rollback)."""
        return self.bash(f'mv "$K/adbkey.{suffix}" "$K/adbkey" && mv "$K/adbkey.pub.{suffix}" "$K/adbkey.pub"')[0] == 0

    def remove_key(self):
        """Removes a key pair this move installed where there was none (a rollback)."""
        return self.bash('rm -f "$K/adbkey" "$K/adbkey.pub"')[0] == 0

    def restart(self):
        """Restarts this user's adb server so it loads the key (`adb kill-server`, never a process kill by pattern)."""
        self.adb('kill-server', timeout=30)
        time.sleep(1)
        return self.adb('start-server', timeout=60)[0] == 0

    def lock_holder(self, path):
        """Who holds a lock file on this machine ('' when free): a flock that cannot be taken, or a file another tool
        writes while it uses the device (its text names the holder)."""
        q = path if path.startswith('"') else shlex.quote(path)
        rc, out = self.bash(f'f={q}; [ -e "$f" ] || exit 0; if flock -n "$f" true 2>/dev/null; then '
                            '[ -s "$f" ] && head -c 200 "$f"; else echo "a running Tutti run"; fi')
        return out.strip() if rc == 0 else ''

    def install_tools(self):
        """Installs Android platform-tools in user space (~/.local/share/tutti/platform-tools) on a Linux machine."""
        body = (f'd={USER_TOOLS}; mkdir -p "$d" && t=$(mktemp -d) && curl -fsSL {PLATFORM_TOOLS_URL} -o "$t/pt.zip" && '
                'python3 -c "import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$t/pt.zip" "$t" && '
                'cp -r "$t/platform-tools/." "$d/" && chmod +x "$d/adb" && rm -rf "$t" && "$d/adb" version | head -1')
        return self.bash(body, timeout=600)
