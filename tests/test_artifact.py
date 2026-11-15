"""A suite's build step as "newest artifact, else a guarded build": an artifact newer than the commit is reused; a build
that is needed waits in its guard hours and while its machine yields."""
import unittest
from types import SimpleNamespace

from tutti import dispatch

CFG = {'machines': {'pc': {'transport': 'ssh', 'yield_file': '/y'}}}
REUSE = {'paths': ['/a.apk', '/b.apk'], 'newer_than': 'git log -1 --format=%ct'}
SUITE = {'project': 'RoRadio', 'name': 'e2e', 'prepare': {'where': 'pc', 'run': 'build', 'guard': {'hours': ['20-24']}, 'reuse': REUSE}}


def answer(newest, commit):
    return lambda m, script, **kw: SimpleNamespace(stdout=f'newest={newest}\ncommit={commit}\n')


class ArtifactTest(unittest.TestCase):
    def p(self):
        return {'commit': 'abc123'}

    def test_an_artifact_newer_than_the_commit_is_reused(self):
        p = self.p()
        why = dispatch.reuse_or_guard(CFG, p, SUITE, 0, sh=answer('200 /b.apk', 100), yield_fn=lambda m: None)
        self.assertIn('reusing /b.apk', why)
        self.assertEqual(p['prepare']['e2e']['status'], 'passed')

    def test_an_older_artifact_means_a_build_unless_guarded(self):
        p = self.p()
        self.assertEqual(dispatch.reuse_or_guard(CFG, p, SUITE, 3600 * 10, sh=answer('50 /a.apk', 100), yield_fn=lambda m: None), '')
        why = dispatch.reuse_or_guard(CFG, p, SUITE, 3600 * 10, sh=answer('50 /a.apk', 100), yield_fn=lambda m: 'pipeline')
        self.assertIn('pc yields', why)
        self.assertEqual(p['prepare']['e2e']['status'], 'failed')   # retried later, the reason shown

    def test_no_build_in_the_owners_evening(self):
        from unittest import mock
        p = self.p()
        with mock.patch('tutti.hours.window', lambda spec, days, now: (0, 1)):
            why = dispatch.reuse_or_guard(CFG, p, SUITE, 0, sh=answer('0 ', ''), yield_fn=lambda m: None)
        self.assertIn('20-24', why)


if __name__ == '__main__':
    unittest.main()
