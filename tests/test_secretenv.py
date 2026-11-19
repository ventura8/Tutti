"""Secrets never travel in a process environment (backlog t116): the masking, the scrub, the point-of-use read and a real
dashboard whose /proc environ holds no TUTTI_*KEY*/TOKEN value even when it was started with one."""
import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
import unittest

import tmpdirs

from tutti import groksetup, secretenv

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAKE = 'fake-test-key-t116'


class SecretEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdirs.mkdtemp()
        self.env = {'XDG_CONFIG_HOME': self.tmp}

    def test_mask_hides_secret_looking_values_only(self):
        m = secretenv.mask({'TUTTI_GROK_KEY': 'v', 'GH_TOKEN': 'v', 'X_SECRET': 'v', 'TUTTI_GROK_URL': 'v', 'PATH': '/bin',
                            'EMPTY_KEY': ''})
        self.assertEqual(m, {'TUTTI_GROK_KEY': '****', 'GH_TOKEN': '****', 'X_SECRET': '****', 'TUTTI_GROK_URL': '****',
                             'PATH': '/bin', 'EMPTY_KEY': ''})

    def test_get_reads_the_file_first_and_scrub_drops_owned_names(self):
        groksetup.save_env(os.path.join(self.tmp, 'tutti', 'grok.env'), 'https://a.cursor.sh/x', FAKE)
        env = {**self.env, 'TUTTI_GROK_KEY': 'old', 'OTHER': '1', 'TUTTI_BROWSER_TOKEN': 'run'}
        self.assertEqual(secretenv.get('TUTTI_GROK_KEY', env), FAKE)
        self.assertEqual(secretenv.get('NOT_THERE', env), '')
        self.assertEqual(secretenv.scrubbed(env), {**self.env, 'OTHER': '1', 'TUTTI_BROWSER_TOKEN': 'run'})
        self.assertEqual(secretenv.scrub_self(env), ['TUTTI_GROK_KEY'])
        self.assertEqual(secretenv.scrub_self(env), [])

    def test_an_explicit_env_without_xdg_never_reads_the_owners_file(self):
        # sealed test bags (scouts tests pass {} or fixture values) must not see ~/.config/tutti/grok.env
        groksetup.save_env(os.path.join(self.tmp, 'tutti', 'grok.env'), 'https://a.cursor.sh/x', FAKE)
        self.assertEqual(secretenv.get('TUTTI_GROK_KEY', {}), '')
        self.assertEqual(secretenv.get('TUTTI_GROK_KEY', {'TUTTI_GROK_KEY': 'from-test'}), 'from-test')

    def test_reexec_only_when_something_was_dropped(self):
        calls = []
        secretenv.reexec_clean(['/x', 'a'], lambda p, a: calls.append(a), {**self.env, 'TUTTI_GROK_KEY': FAKE})
        secretenv.reexec_clean(['/x', 'a'], lambda p, a: calls.append(a), dict(self.env))
        self.assertEqual(calls, [['/x', 'a']])

    def test_doctor_names_an_exported_key_never_its_value(self):
        p = secretenv.doctor_problems({**self.env, 'TUTTI_GROK_KEY': FAKE})
        self.assertIn('TUTTI_GROK_KEY', p[0])
        self.assertNotIn(FAKE, p[0])
        self.assertEqual(secretenv.doctor_problems(dict(self.env)), [])


def marked(mark):
    """The pids whose environment carries `mark`: the dashboard and everything it started, a daemonised ssh included."""
    out = []
    for d in os.listdir('/proc'):
        try:
            with open(f'/proc/{d}/environ', 'rb') as f:
                if mark in f.read():
                    out.append(int(d))
        except (OSError, ValueError):
            continue
    return out


def stop_all(proc, mark):
    deadline = time.time() + 10
    while time.time() < deadline:
        pids = [p for p in marked(mark) if p != os.getpid()]
        if not pids:
            break
        for p in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(p, signal.SIGKILL)
        time.sleep(0.1)
    proc.wait(10)


@unittest.skipUnless(os.path.isdir('/proc/self'), 'needs /proc')
class DashboardEnviron(unittest.TestCase):
    def test_a_dashboard_started_with_the_key_has_none_in_its_proc_environ(self):
        sys.path.insert(0, os.path.join(HERE, 'e2e'))
        import serve
        home = tmpdirs.mkdtemp()
        cfg = serve.write_config(home)
        mark = f'm{os.getpid()}-{time.time_ns()}'
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        env = {**os.environ, 'TUTTI_NO_DAEMONS': '1', 'HOME': home, 'XDG_CONFIG_HOME': os.path.join(home, '.config'),
               'TUTTI_TELEMETRY': 'off', 'PYTHONPATH': ROOT, 'TMPDIR': home, 'T116_MARK': mark,
               'TUTTI_GROK_KEY': FAKE, 'TUTTI_GROK_URL': 'https://a.cursor.sh/x'}
        proc = subprocess.Popen([sys.executable, '-c', 'import sys; from tutti import cli; sys.exit(cli.main())', '--config', cfg,
                                 'dashboard', '--port', str(port)], env=env, cwd=home, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(stop_all, proc, mark.encode())   # the dashboard and every probe it started
        deadline, seen = time.time() + 30, None
        while time.time() < deadline and proc.poll() is None:
            try:
                with socket.create_connection(('127.0.0.1', port), 0.5):
                    pass
                with open(f'/proc/{proc.pid}/environ', 'rb') as f:
                    seen = f.read()
                break
            except OSError:
                time.sleep(0.2)
        self.assertIsNotNone(seen, f'the dashboard did not serve (exit {proc.poll()})')
        names = [e.split(b'=', 1)[0].decode() for e in seen.split(b'\0') if e]
        self.assertFalse([n for n in names if n.startswith('TUTTI_') and ('KEY' in n or 'TOKEN' in n)], names)
        self.assertNotIn(FAKE.encode(), seen)


if __name__ == '__main__':
    unittest.main()
