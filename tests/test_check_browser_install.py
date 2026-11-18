"""The CI browser installer only skips apt after an actual successful launch."""
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'check-browser-install.sh'


class BrowserInstallTest(unittest.TestCase):
    def run_installer(self, *, probe_ok=True, install_ok=True, fallback_ok=True, ci=True, browser_path=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            log = root / 'calls'
            fake = root / 'python3'
            fake.write_text(f'''#!/bin/sh
if [ "$1" = -c ]; then
  printf '%s\\n' "-c probe path=${{PLAYWRIGHT_BROWSERS_PATH+set:$PLAYWRIGHT_BROWSERS_PATH}}" >> '{log}'
else printf '%s\\n' "$*" >> '{log}'; fi
case "$1 $2 $3 $4 $5" in
  *--with-deps*) exit {0 if fallback_ok else 1} ;;
  -m\\ playwright*) exit {0 if install_ok else 1} ;;
  *) exit {0 if probe_ok else 1} ;;
esac
''')
            fake.chmod(0o755)
            env = {'PATH': f'{root}:/usr/bin:/bin', 'HOME': folder, 'TMPDIR': folder}
            if ci:
                env['CI'] = 'true'
            if browser_path is not None:
                env['PLAYWRIGHT_BROWSERS_PATH'] = browser_path
            result = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True, timeout=15, check=False)
            return result.returncode, log.read_text().splitlines()

    def test_successful_launch_avoids_system_install(self):
        status, calls = self.run_installer()
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn('--only-shell chromium', calls[0])
        self.assertTrue(calls[1].startswith('-c '))

    def test_failed_launch_installs_dependencies(self):
        status, calls = self.run_installer(probe_ok=False)
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 3)
        self.assertIn('--with-deps', calls[2])

    def test_probe_preserves_browser_path_only_when_set(self):
        for path, expected in ((None, '-c probe path='), ('/custom/cache', '-c probe path=set:/custom/cache'),
                               ('', '-c probe path=set:')):
            with self.subTest(path=path):
                status, calls = self.run_installer(browser_path=path)
                self.assertEqual(status, 0)
                self.assertEqual(calls[1], expected)

    def test_failed_dependency_install_fails_check(self):
        status, calls = self.run_installer(probe_ok=False, fallback_ok=False)
        self.assertNotEqual(status, 0)
        self.assertIn('--with-deps', calls[-1])

    def test_failed_browser_install_does_not_probe_or_succeed(self):
        status, calls = self.run_installer(install_ok=False)
        self.assertNotEqual(status, 0)
        self.assertEqual(len(calls), 1)

    def test_local_install_does_not_probe_or_install_system_packages(self):
        status, calls = self.run_installer(ci=False)
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 1)
