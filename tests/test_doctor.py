import os
import stat
import tempfile
import unittest

from tutti import doctor


class DoctorTest(unittest.TestCase):
    def test_loader_error_is_a_problem(self):
        out = 'git-remote-https: error while loading shared libraries: libcurl.so.4: version `CURL_4' + "'" + ' not found'
        msg = doctor.git_https_problem('/opt/x/git', False, out)
        self.assertIn('cannot do HTTPS', msg)
        self.assertIn('PATH', msg)

    def test_usage_output_is_fine(self):
        self.assertIsNone(doctor.git_https_problem('/usr/bin/git', True, 'usage: git remote-curl'))

    def test_healthy_needs_the_usage_answer(self):
        self.assertTrue(doctor.healthy(1, 'error: remote-curl: usage: git remote-curl <remote> [<url>]'))
        self.assertFalse(doctor.healthy(-11, ''))
        self.assertFalse(doctor.healthy(127, 'Error loading shared library libcurl.so.4: No such file'))
        self.assertFalse(doctor.healthy(1, 'Error relocating /usr/libexec/git-core/git-remote-https: curl_x: symbol not found'))
        self.assertFalse(doctor.healthy(1, "git: 'remote-https' is not a git command"))

    def test_real_git_probe(self):
        if os.path.exists('/usr/bin/git'):
            ok, out = doctor.probe_https('/usr/bin/git')
            self.assertTrue(ok, out)
        self.assertFalse(doctor.probe_https('/nonexistent/git')[0])

    def test_check_names_a_working_git(self):
        with tempfile.TemporaryDirectory() as t:
            dirs = []
            for n in ('bad', 'good'):
                d = os.path.join(t, n)
                os.mkdir(d)
                g = os.path.join(d, 'git')
                open(g, 'w').close()
                os.chmod(g, stat.S_IRWXU)
                dirs.append(d)
            path = os.pathsep.join(dirs)

            def probe(git):
                return ('good' in git, '' if 'good' in git else 'error while loading shared libraries: libcurl')

            out = doctor.check_git(path, probe)
            self.assertEqual(len(out), 1)
            self.assertIn(os.path.join(t, 'good', 'git') + ' works', out[0])
            self.assertEqual(doctor.check_git(dirs[1], probe), [])

    def test_no_git(self):
        self.assertIn('no git', doctor.check_git('/nonexistent')[0])


if __name__ == '__main__':
    unittest.main()
