"""Small pure pieces: the brief, the remote command lines, waits, the translation validators and the QA gate."""
import base64
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tutti import config, local, remote, translate, waiting, workers


def cfg_of(raw):
    return config.normalise(raw)


class BriefTest(unittest.TestCase):
    def test_brief_carries_task_files_project_and_standing_rules(self):
        text = workers.brief(cfg_of({'brief': {'rules': ['Follow AGENTS.md.']}}), 'Fix the typo', ['a.py'])
        for part in ('Fix the typo', '- a.py', '- Follow AGENTS.md.', 'Do not commit'):
            self.assertIn(part, text)

    def test_prompt_is_one_argument(self):
        cmd = workers.command({'command': ['agent', '-p', '{prompt}', '--trust']}, 'a "quoted"; task')
        self.assertEqual(cmd, ['agent', '-p', 'a "quoted"; task', '--trust'])

    def test_missing_cli_worker_says_how_to_fix_it(self):
        ok, note = workers.available({'kind': 'cli', 'command': ['tutti-no-such-binary']})
        self.assertFalse(ok)
        self.assertIn('not on PATH', note)

    def _tags_response(self, models):
        body = io.BytesIO(json.dumps({'models': models}).encode())
        cm = mock.MagicMock()
        cm.__enter__.return_value = body
        cm.__exit__.return_value = False
        return cm

    def test_ollama_worker_is_ready_only_when_its_model_is_listed(self):
        worker = {'kind': 'ollama', 'host': '127.0.0.1:11434', 'model': 'coder:9b'}
        with mock.patch('tutti.workers.urllib.request.urlopen', return_value=self._tags_response(
                [{'name': 'coder:9b'}, {'name': 'other:1b'}])) as urlopen:
            ok, note = workers.available(worker)
        self.assertTrue(ok)
        self.assertIn('coder:9b at 127.0.0.1:11434', note)
        urlopen.assert_called_once()
        self.assertIn('/api/tags', urlopen.call_args.args[0])

    def test_ollama_worker_names_a_missing_model_and_the_pull(self):
        worker = {'kind': 'ollama', 'host': '127.0.0.1:11434', 'model': 'coder:9b'}
        with mock.patch('tutti.workers.urllib.request.urlopen', return_value=self._tags_response([{'name': 'other:1b'}])):
            ok, note = workers.available(worker)
        self.assertFalse(ok)
        self.assertIn('model missing: coder:9b', note)
        self.assertIn('OLLAMA_HOST=127.0.0.1:11434 ollama pull coder:9b', note)

    def test_ollama_untagged_model_matches_latest(self):
        worker = {'kind': 'ollama', 'host': '127.0.0.1:11434', 'model': 'llama3'}
        with mock.patch('tutti.workers.urllib.request.urlopen', return_value=self._tags_response([{'name': 'llama3:latest'}])):
            ok, _ = workers.available(worker)
        self.assertTrue(ok)

    def test_ollama_tags_of_another_shape_are_down_not_a_crash(self):
        worker = {'kind': 'ollama', 'host': '127.0.0.1:11434', 'model': 'coder:9b'}
        for body in (b'[]', b'{"models": [null]}', b'{"models": 3}'):
            cm = self._tags_response([])
            cm.__enter__.return_value = io.BytesIO(body)
            with mock.patch('tutti.workers.urllib.request.urlopen', return_value=cm):
                ok, note = workers.available(worker)
            self.assertFalse(ok, body)

    def test_ollama_url_keeps_a_written_scheme(self):
        self.assertEqual(config.ollama_url('127.0.0.1:11434', '/api/tags'), 'http://127.0.0.1:11434/api/tags')
        self.assertEqual(config.ollama_url('https://gpu.lan/', '/api/tags'), 'https://gpu.lan/api/tags')

    def test_ollama_worker_says_when_the_answer_is_not_json(self):
        worker = {'kind': 'ollama', 'host': '127.0.0.1:11434', 'model': 'coder:9b'}
        cm = self._tags_response([])
        cm.__enter__.return_value = io.BytesIO(b'<html>')
        with mock.patch('tutti.workers.urllib.request.urlopen', return_value=cm):
            ok, note = workers.available(worker)
        self.assertFalse(ok)
        self.assertIn('not an ollama model list', note)

    def test_ollama_worker_down_when_the_server_does_not_answer(self):
        worker = {'kind': 'ollama', 'host': '127.0.0.1:9', 'model': 'coder:9b'}
        with mock.patch('tutti.workers.urllib.request.urlopen', side_effect=OSError('refused')):
            ok, note = workers.available(worker)
        self.assertFalse(ok)
        self.assertIn('does not answer', note)


class RemoteTest(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_of({'machines': {
            'a': {'host': 'a.example', 'slots': 2,
                  'runner': {'kind': 'docker', 'image': 'img:1', 'env': {'K': 'v w'}, 'prelude': 'mkdir -p $HOME'}},
            'w': {'host': 'w.example', 'transport': 'wsl', 'wsl_helper': '/home/u/run64.sh'},
            'l': {'host': 'local', 'locks': ['/tmp/one.lock']}}})

    def test_transports(self):
        m = self.cfg['machines']
        with mock.patch.object(remote.os, 'name', 'posix'):
            self.assertEqual(remote.argv(m['l'], 'true'), ['bash', '-c', 'true'])
        self.assertEqual(remote.argv(m['a'], 'true')[-2:], ['a.example', 'true'])
        line = remote.argv(m['w'], "echo 'it works'")[-1]
        self.assertTrue(line.startswith('wsl -e /home/u/run64.sh '))
        self.assertNotIn("'", line)   # nothing left for Windows' shell to mangle

    def test_local_on_windows_sends_the_line_without_quotes(self):
        # Windows rebuilt `cut -d" "` as `cut -d\"` for bash.exe: the local probe failed and `here` showed unreachable.
        script = 'echo "a b" | cut -d" " -f1'
        with mock.patch.object(remote.os, 'name', 'nt'):
            args = remote.argv(self.cfg['machines']['l'], script)
        self.assertEqual(args[:2], ['bash', '-c'])
        for mangled in ('"', '&&', '$(', '{'):   # what Windows' bash.exe broke on (rounds 1 and 2)
            self.assertNotIn(mangled, args[2])
        packed = args[2].split('echo ')[1].split()[0]
        self.assertEqual(base64.b64decode(packed).decode(), script)
        self.assertNotIn('| bash', args[2])  # sourced, not piped: the command keeps the caller's stdin

    @unittest.skipUnless(os.name == 'nt', 'what Windows bash.exe does to the line')
    def test_local_line_keeps_stdin_quotes_and_exit_code_on_windows(self):
        r = remote.sh({'transport': 'local'}, 'read x; echo "got:$x" | cut -d":" -f2; exit 7', capture=True, stdin='hello\n')
        self.assertEqual((r.returncode, r.stdout.strip()), (7, 'hello'))

    def test_docker_runner_is_capped_and_quotes_the_command(self):
        line = remote.runner_command(self.cfg['machines']['a'], 'trk', "echo 'hi' && exit 3")
        for part in ('--memory=12g', '--memory-swap=12g', "-e 'K=v w'", 'img:1', 'tutti-a-trk-'):
            self.assertIn(part, line)
        self.assertIn('-ge 85000 ] && echo 1 || echo 8)"', line)   # the default heat.cap_c: read at start, 8 when cool
        self.assertIn('mkdir -p $HOME && echo', line)

    def test_run_label_is_validated_and_observation_never_proves_exit(self):
        identity = 'a' * 32
        line = remote.runner_command(self.cfg['machines']['a'], 'trk', 'true', identity=identity)
        self.assertIn('--label io.tutti.run=' + identity, line)
        bad = 'bad;$(touch /tmp/no)'
        self.assertNotIn(bad, remote.runner_command(self.cfg['machines']['a'], 'trk', 'true', identity=bad))
        row = {'id': 'a' * 64, 'identity': identity, 'running': True}
        self.assertEqual(remote.container_observation(identity, [row], True)['state'], 'observed')
        self.assertEqual(remote.container_observation(identity, [], True)['state'], 'unknown')
        self.assertEqual(remote.container_observation(identity, [row], False)['state'], 'unknown')
        self.assertEqual(remote.container_observation(identity, [row], 'false')['state'], 'unknown')
        self.assertEqual(remote.container_observation(identity, [{**row, 'id': '../bad'}], True)['state'], 'unknown')
        self.assertEqual(remote.container_observation(identity, [{**row, 'running': False}], True)['state'], 'unknown')

    def test_run_propagates_identity_to_docker_without_running_a_machine(self):
        machine = {**self.cfg['machines']['a'], 'transport': 'local'}
        captured = []
        def fake_sh(_machine, script):
            captured.append(script)
            return subprocess.CompletedProcess([], 0)
        with mock.patch.dict(os.environ, {'TUTTI_RUN_IDENTITY': 'a' * 32}), \
                mock.patch.object(remote, 'sh', side_effect=fake_sh), \
                mock.patch.object(remote.worktree, 'docker_mount', return_value=''):
            self.assertEqual(remote.run(self.cfg, machine, 'track', '/tmp/fake-tree', 'true'), 0)
        self.assertIn('--label io.tutti.run=' + 'a' * 32, captured[0])

    def test_remote_track_rejected_before_transport_or_filesystem(self):
        machine = {**self.cfg['machines']['a'], 'transport': 'ssh'}
        for bad in ('../escape', 'bad;echo x', 'name\n', '', 'x' * 65, 'repo.git', 'TUTTI-SLOT.SH'):
            with self.subTest(bad=bad), mock.patch.object(remote, 'sh') as sh, \
                    mock.patch.object(remote.subprocess, 'run') as run:
                for operation in (lambda value=bad: remote.run(self.cfg, machine, value, '/tmp/tree', 'true'),
                                  lambda value=bad: remote.sync(self.cfg, machine, value, '/tmp/tree'),
                                  lambda value=bad: remote.fetch(self.cfg, machine, value, '/tmp/tree', 'result')):
                    with self.assertRaisesRegex(RuntimeError, 'remote track must be one folder'):
                        operation()
                sh.assert_not_called()
                run.assert_not_called()
        self.assertTrue(remote.valid_track('safe-1_name.test'))
        docker = {**self.cfg['machines']['a'], 'transport': 'local'}
        with mock.patch.object(remote, 'sh') as sh:
            with self.assertRaisesRegex(RuntimeError, 'remote track must be one folder'):
                remote.run(self.cfg, docker, 'bad;echo x', '/tmp/tree', 'true')
            sh.assert_not_called()

    def test_copy_name_is_unique_per_run_and_cleanup_skips_local(self):
        self.assertEqual(remote.copy_name('tutti-run', '1795017582-123'), 'tutti-run-1795017582-123')
        with self.assertRaisesRegex(RuntimeError, 'too long or unsafe'):
            remote.copy_name('x' * 50, '1795017582-123456789')
        machine = {**self.cfg['machines']['a'], 'transport': 'ssh', 'root': '~/tutti'}
        with mock.patch.object(remote, 'sh', return_value=subprocess.CompletedProcess([], 0)) as sh:
            self.assertEqual(remote.cleanup_copy(self.cfg, machine, 'tutti-run-1-2'), 0)
        self.assertIn('rm -rf --', sh.call_args.args[1])
        self.assertIn('tutti-run-1-2', sh.call_args.args[1])
        local = {**self.cfg['machines']['l'], 'transport': 'local'}
        with mock.patch.object(remote, 'sh') as sh:
            self.assertEqual(remote.cleanup_copy(self.cfg, local, 'tutti-run-1-2'), 0)
            sh.assert_not_called()
        with mock.patch.object(remote, 'sh') as sh:
            with self.assertRaisesRegex(RuntimeError, 'remote track must be one folder'):
                remote.cleanup_copy(self.cfg, machine, 'repo.git')
            sh.assert_not_called()

    def test_sync_bounds_hanging_copy_steps(self):
        machine = {**self.cfg['machines']['a'], 'transport': 'ssh', 'root': '~/tutti'}
        with mock.patch.object(remote, 'sh', return_value=subprocess.CompletedProcess([], 0)), \
                mock.patch.object(remote.worktree, 'git_bin', return_value='git'), \
                mock.patch.object(remote.subprocess, 'run',
                                  side_effect=[subprocess.CompletedProcess([], 0),
                                               subprocess.TimeoutExpired('git', remote.SYNC_TIMEOUT)]):
            with self.assertRaisesRegex(RuntimeError, f'timed out after {remote.SYNC_TIMEOUT}s'):
                remote.sync(self.cfg, machine, 'tutti-run-1-2', '/tmp/tree')

    def test_local_shell_track_may_contain_spaces(self):
        machine = {**self.cfg['machines']['l'], 'runner': {'kind': 'shell'}}
        with mock.patch.object(remote, 'sh', return_value=subprocess.CompletedProcess([], 0)) as sh:
            self.assertEqual(remote.run(self.cfg, machine, 'local tree', '/tmp/fake-tree', 'true'), 0)
        sh.assert_called_once()
        self.assertEqual(remote.fetch(self.cfg, machine, 'local tree', '/tmp/fake-tree', 'result'), 0)

    def test_lock_files(self):
        m = self.cfg['machines']
        self.assertEqual(remote.lock_files(m['a']), ['/tmp/tutti-slot-1.lock', '/tmp/tutti-slot-2.lock'])
        self.assertEqual(remote.lock_files(m['l']), ['/tmp/one.lock'])
        self.assertIn('/tmp/one.lock', remote.probe_script(m['l']))

    def test_home_paths(self):
        self.assertEqual(remote.home_path('~/.tutti/yield'), '"$HOME"/.tutti/yield')
        self.assertEqual(remote.home_path('/mnt/c/a b.txt'), "'/mnt/c/a b.txt'")

    def test_local_probe_and_run_and_yield(self):
        # flock needs a POSIX filesystem: a Windows tempfile path (often /mnt/c/...) reports free=0 under WSL/Git Bash.
        # Locks and the yield flag live under bash's /tmp; the work tree stays in the Windows-visible temp dir.
        with tempfile.TemporaryDirectory() as d:
            lock_dir = subprocess.check_output(['bash', '-c', 'mktemp -d "${TMPDIR:-/tmp}/tutti-test.XXXXXX"'], text=True).strip()
            try:
                cfg = cfg_of({'machines': {'l': {'host': 'local', 'min_free_gb': 0,
                                                 'locks': [f'{lock_dir}/s.lock'], 'yield_file': f'{lock_dir}/yield'}}})
                m = cfg['machines']['l']
                p = remote.probe(m)
                self.assertEqual((p.reachable, p.free, p.yielding), (True, 1, False))
                self.assertEqual(remote.run(cfg, m, 't', d, 'echo ran > out.txt; exit 7'), 7)
                self.assertTrue(os.path.exists(f'{d}/out.txt'))
                self.assertEqual(remote.set_yield(m, True, 'unload: release build'), 0)
                self.assertTrue(remote.probe(m).yielding)
                self.assertEqual(remote.yield_note(m), 'unload: release build')
                self.assertEqual(remote.set_yield(m, False), 0)
                self.assertIsNone(remote.yield_note(m))
            finally:
                subprocess.run(['bash', '-c', 'rm -rf "$1"', 'bash', lock_dir], check=False)


class WaitingTest(unittest.TestCase):
    def test_until_returns_true_at_once_and_false_at_the_timeout(self):
        clock = iter(range(100))
        self.assertTrue(waiting.until(lambda: True, 5, clock=lambda: next(clock), sleep=lambda s: None))
        self.assertFalse(waiting.until(lambda: False, 3, clock=lambda: next(clock), sleep=lambda s: None))

    def test_checks(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'run.log')
            self.assertFalse(waiting.file_exists(path)())
            self.assertFalse(waiting.file_matches(path, 'passed')())
            with open(path, 'w', encoding='utf8') as f:
                f.write('stage static passed\n')
            self.assertTrue(waiting.file_matches(path, r'stage \w+ passed')())
        self.assertFalse(waiting.pid_gone(os.getpid())())
        self.assertTrue(waiting.pid_gone(2_000_000_000)())


class LocalTest(unittest.TestCase):
    def test_only_named_files_are_written(self):
        with tempfile.TemporaryDirectory() as d:
            answer = '=== FILE: a.py ===\n```python\nx = 1\n```\n=== FILE: ../evil.py ===\n```\nboom\n```\n'
            self.assertEqual(local.apply_blocks(answer, ['a.py'], d), ['a.py'])
            self.assertEqual(os.listdir(d), ['a.py'])


class TranslateTest(unittest.TestCase):
    def test_validators(self):
        v = translate.validate
        self.assertEqual(v('Save {count} files.', 'Enregistrer {count} fichiers.'), [])
        self.assertIn('placeholders', v('Save {count} files.', 'Enregistrer {nombre} fichiers.')[0])
        self.assertIn('end punctuation', v('Save?', 'Enregistrer.')[0])
        self.assertIn('untranslated', v('Save the file', 'Save the file')[0])
        self.assertEqual(v('Tutti', 'Tutti', keep=['Tutti']), [])
        self.assertIn('must stay', v('Open Tutti', 'Ouvrir Tous', keep=['Tutti'])[0])
        self.assertEqual(v('Done', ''), ['the translation is empty'])

    def test_unreadable_judge_answer_fails(self):
        self.assertEqual(translate.verdict('OK. Same information.'), 'OK')
        self.assertEqual(translate.verdict('minor: wording'), 'MINOR')
        self.assertEqual(translate.verdict('I think it is fine'), 'WRONG')

    def test_only_qa_passed_strings_are_written_and_the_gate_notices_edits(self):
        def gen(role, prompt):
            if role == 'translator':
                return 'Enregistrer.' if prompt.endswith('Save.') else 'Mauvais'
            return 'Save.' if 'Translate this' in prompt else ('OK' if 'Original: Save.' in prompt else 'WRONG: other meaning')
        cfg = {'workers': {'t': {}, 'j': {}}, 'translate': {'translator': 't', 'judge': 'j', 'retries': 1}}
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'en.json')
            with open(src, 'w', encoding='utf8') as f:
                json.dump({'save': 'Save.', 'bad': 'Delete everything'}, f)
            self.assertEqual(translate.run(cfg, src, 'fr', d, gen), (1, 1))
            out = os.path.join(d, 'fr.json')
            self.assertEqual(translate.load(out), {'save': 'Enregistrer.'})
            self.assertEqual(translate.check(out), '')
            self.assertEqual(translate.run(cfg, src, 'fr', d, lambda r, p: 'Mauvais' if r == 'translator' else 'WRONG'), (1, 1))
            with open(out, 'a', encoding='utf8') as f:
                f.write(' ')
            self.assertIn('changed after its QA', translate.check(out))
            os.remove(os.path.join(d, 'fr.qa.json'))
            self.assertIn('no QA report', translate.check(out))


if __name__ == '__main__':
    unittest.main()
