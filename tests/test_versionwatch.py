"""A dashboard whose installed release differs from the one it runs restarts at the next safe point, shows a banner, and
never loops (fakes only: no real dashboard, no execv)."""
import os
import tempfile
import unittest

from test_codewatch import Inline

from tutti import install


class Stop:
    """An event stand-in: wait() lets `rounds` loop turns run, then says stop."""

    def __init__(self, rounds):
        self.rounds = rounds

    def wait(self, _every):
        self.rounds -= 1
        return self.rounds < 0


class Release(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.home = d.name
        self.old = os.path.join(self.home, 'versions', '0.1.0')
        os.makedirs(os.path.join(self.old, 'tutti'))
        self.new = os.path.join(self.home, 'versions', '0.2.0')
        os.makedirs(os.path.join(self.new, 'tutti'))
        for folder, v in ((self.old, '0.1.0'), (self.new, '0.2.0')):
            with open(os.path.join(folder, 'tutti', '__init__.py'), 'w') as f:
                f.write(f"__version__ = '{v}'\n")
        install.point_current(self.home, '0.1.0')

    def watch(self, restart, rounds=3, env=None, running='0.1.0'):
        env = {} if env is None else env
        install.start_version_watch(Stop(rounds), self.old, restart, running=running, home_dir=self.home, every=0, env=env,
                                    thread=Inline)
        return env

    def test_reads_the_installed_version(self):
        self.assertEqual(install.installed_version(self.home), '0.1.0')
        install.point_current(self.home, '0.2.0')
        self.assertEqual(install.installed_version(self.home), '0.2.0')

    def test_no_current_or_unreadable_file_is_none(self):
        self.assertIsNone(install.installed_version(os.path.join(self.home, 'nowhere')))
        with open(os.path.join(self.new, 'tutti', '__init__.py'), 'w') as f:
            f.write('no version here\n')
        install.point_current(self.home, '0.2.0')
        self.assertIsNone(install.installed_version(self.home))

    def test_same_version_never_restarts(self):
        calls = []
        self.watch(lambda: calls.append(1))
        self.assertEqual(calls, [])
        self.assertIsNone(install.restart_pending(self.old, '0.1.0', self.home))

    def test_new_release_restarts_once(self):
        install.point_current(self.home, '0.2.0')
        calls = []
        env = self.watch(lambda: calls.append(1))
        self.assertEqual(calls, [1])
        self.assertEqual(env[install.RESTARTED_ENV], '0.2.0')

    def test_busy_chat_defers_then_retries(self):
        install.point_current(self.home, '0.2.0')
        answers = iter([False, False, None])
        calls = []

        def restart():
            calls.append(1)
            return next(answers)
        self.watch(restart, rounds=5)
        self.assertEqual(len(calls), 3)

    def test_a_restart_that_did_not_help_is_not_repeated(self):
        install.point_current(self.home, '0.2.0')
        calls = []
        self.watch(lambda: calls.append(1), env={install.RESTARTED_ENV: '0.2.0'})
        self.assertEqual(calls, [])

    def test_banner_names_both_versions_only_for_release_installs(self):
        install.point_current(self.home, '0.2.0')
        p = install.restart_pending(self.old, '0.1.0', self.home)
        self.assertEqual((p['running'], p['installed']), ('0.1.0', '0.2.0'))
        self.assertIn('0.2.0', p['text'])
        self.assertIsNone(install.restart_pending(tempfile.gettempdir(), '0.1.0', self.home))   # not under versions/


if __name__ == '__main__':
    unittest.main()
