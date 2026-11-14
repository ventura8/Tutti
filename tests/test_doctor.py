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
        self.assertIsNone(doctor.git_https_problem('/usr/bin/git', False, 'some other failure'))

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
