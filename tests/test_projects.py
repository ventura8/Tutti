"""Projects for the dashboard: a folder's repository, the jobs and machine processes per project, RoRadio's Windows side."""
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tutti import config, projects

GIT_CONFIG = ('[core]\n\tbare = false\n[remote "upstream"]\n\turl = https://example.com/x/other.git\n'
              '[remote "origin"]\n\turl = git@example.com:me/the-map.git\n')


def repo(root, name='main', worktree=None):
    """A main checkout with .git files (and a linked worktree when named), without running git."""
    main = os.path.join(root, name)
    g = os.path.join(main, '.git')
    os.makedirs(os.path.join(g, 'worktrees', 'wt'))
    with open(os.path.join(g, 'config'), 'w', encoding='utf8') as f:
        f.write(GIT_CONFIG)
    with open(os.path.join(g, 'HEAD'), 'w', encoding='utf8') as f:
        f.write('ref: refs/heads/master\n')
    if worktree:
        os.makedirs(worktree)
        with open(os.path.join(worktree, '.git'), 'w', encoding='utf8') as f:
            f.write(f'gitdir: {os.path.join(g, "worktrees", "wt")}\n')
        with open(os.path.join(g, 'worktrees', 'wt', 'commondir'), 'w', encoding='utf8') as f:
            f.write('../..\n')
        with open(os.path.join(g, 'worktrees', 'wt', 'HEAD'), 'w', encoding='utf8') as f:
            f.write('0123456789abcdef\n')
    return main


def cfg_of(folder, **over):
    cfg = config.normalise({'project': {'worktrees': os.path.join(folder, 'wt'), 'state': os.path.join(folder, 'state')},
                            'machines': {'nuc': {'host': 'nuc.example', 'root': 'proj-wt'},
                                         'pc': {'host': 'pc.example', 'transport': 'wsl', 'wsl_helper': '/x'}},
                            'projects': {'RoRadio': {'match': 'roradio', 'machines': ['pc'], 'windows': {'pc': 'C:\\me\\RoRadio'},
                                                     'containers': ['ro-']}}, **over})
    cfg['_path'] = os.path.join(folder, 'tutti.config.yaml')
    return cfg


class IdentifyTest(unittest.TestCase):
    def test_a_worktree_leads_to_its_main_checkout_and_origin(self):
        with tempfile.TemporaryDirectory() as d:
            main = repo(d, worktree=os.path.join(d, 'wt', 'track'))
            found = projects.identify(os.path.join(d, 'wt', 'track'))
            self.assertEqual((found['name'], found['repo'], found['branch']), ('the-map', main, 'detached 01234567'))
            self.assertEqual(projects.identify(os.path.join(main, 'sub'))['branch'], 'master')
            self.assertIsNone(projects.identify(d))

    def test_names_from_remotes(self):
        self.assertEqual(projects.name_from('https://github.com/o/Tutti.git', '/x'), 'Tutti')
        self.assertEqual(projects.name_from('', '/home/me/app/'), 'app')
        self.assertEqual(projects.origin_url('[remote "a"]\n url = u1\n'), 'u1')
        self.assertEqual(projects.origin_url('[core]\n'), '')

    def test_a_broken_git_file_is_no_repository(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, '.git'), 'w', encoding='utf8') as f:
                f.write('nonsense')
            self.assertEqual(projects.git_dir(os.path.join(d, 'x')), (None, None))


class TagTest(unittest.TestCase):
    def test_jobs_get_their_project(self):
        with tempfile.TemporaryDirectory() as d:
            repo(d, worktree=os.path.join(d, 'wt', 'a'))
            cfg = cfg_of(d)
            jobs = [{'kind': 'agent', 'cwd': os.path.join(d, 'wt', 'a'), 'status': 'running'},
                    {'kind': 'remote', 'title': 'ro-test-1 on nuc', 'status': 'running'},
                    {'kind': 'agent task', 'path': '/tmp/x/-home-me-RoRadio/s/tasks/b.output', 'status': 'ended'},
                    {'kind': 'quiet', 'log': '/state/quiet/1-2.log', 'status': 'done'}]
            projects.tag(cfg, jobs)
            self.assertEqual([j['project'] for j in jobs], ['the-map', 'RoRadio', 'RoRadio', None])

    def test_a_running_delegated_run_is_found_by_its_start_record(self):
        with tempfile.TemporaryDirectory() as d:
            main = repo(d)
            cfg = cfg_of(d)
            run_dir = os.path.join(cfg['project']['state'], 'runs', '20260101-000000-1')
            os.makedirs(run_dir)
            with open(os.path.join(run_dir, 'meta.json'), 'w', encoding='utf8') as f:
                json.dump({'repo': main, 'worktree': '/gone'}, f)
            jobs = projects.tag(cfg, [{'kind': 'run', 'log': run_dir + '.log', 'status': 'running'}])
            self.assertEqual(jobs[0]['project'], 'the-map')

    def test_a_delegated_run_is_found_by_its_report(self):
        with tempfile.TemporaryDirectory() as d:
            repo(d, worktree=os.path.join(d, 'wt', 'tutti-run-1'))
            cfg = cfg_of(d)
            with mock.patch('tutti.delegate.load_report', return_value={'worktree': os.path.join(d, 'wt', 'tutti-run-1')}):
                jobs = projects.tag(cfg, [{'kind': 'run', 'log': '/s/runs/20260101-1.log', 'status': 'done'}])
            self.assertEqual(jobs[0]['project'], 'the-map')


class ActivityTest(unittest.TestCase):
    def test_the_script_reads_only_and_parses(self):
        script = projects.activity_script()
        for word in ('kill', 'rm -rf', 'docker stop'):
            self.assertNotIn(word, script)
        text = ('W\t/h/p\tnode\nW\t/h/p\tbash\nW\t/h/q\tvim\n'
                'R\t/h/p\t/h/p\tmain\thttps://x/RoRadio.git\t/h/p/.git\nG 37 1024 8192\nnoise\n')
        a = projects.parse_activity(text)
        self.assertEqual(a['procs'], [('/h/p', 'node'), ('/h/p', 'bash'), ('/h/q', 'vim')])
        self.assertEqual(a['repos']['/h/p']['repo'], '/h/p')
        self.assertEqual(a['gpu'], {'util_pct': 37.0, 'mem_used_mib': 1024.0, 'mem_total_mib': 8192.0})
        self.assertIsNone(projects.parse_gpu('G n/a 1 2'))

    def test_this_computer_answers_the_script(self):
        a = projects.probe_activity({'transport': 'local', 'name': 'here'})
        self.assertNotIn('error', a)
        self.assertTrue(a['procs'] or a['repos'] == {})

    def test_a_process_in_a_folder_under_home_is_found_by_its_program(self):
        with tempfile.TemporaryDirectory() as home:
            work = os.path.join(home, 'proj')
            os.makedirs(work)
            p = subprocess.Popen(['sleep', '30'], cwd=work)
            try:
                with mock.patch.dict(os.environ, {'HOME': home}):
                    a = projects.probe_activity({'transport': 'local', 'name': 'here'})
            finally:
                p.kill()
                p.wait()
            self.assertIn((os.path.realpath(work), 'sleep'), [(os.path.realpath(c), n) for c, n in a['procs']])
            self.assertTrue(all(os.path.realpath(c).startswith(os.path.realpath(home)) for c, _ in a['procs']))

    def test_a_machine_that_does_not_answer_says_so(self):
        with mock.patch('tutti.remote.sh', side_effect=subprocess.TimeoutExpired('ssh', 1)):
            self.assertIn('did not answer', projects.probe_activity({'transport': 'ssh', 'host': 'x'}, timeout=1)['error'])
        with mock.patch('tutti.remote.sh', return_value=subprocess.CompletedProcess([], 255, '', 'refused')):
            self.assertIn('exited 255', projects.probe_activity({'transport': 'ssh', 'host': 'x'})['error'])

    def test_the_windows_side_is_read_for_configured_folders(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d)
            self.assertEqual(projects.windows_folders(cfg, 'pc'), [('RoRadio', 'C:\\me\\RoRadio')])
            script = projects.windows_script([("Ro'Radio", 'C:\\me\\RoRadio')])
            self.assertIn("'Ro''Radio'='C:\\me\\RoRadio'", script)
            self.assertNotIn('Stop-Process', script)
            w = projects.parse_windows('X\tRoRadio\tdotnet.exe\t2026-01-01T10:00:00+02:00\r\nB\tRoRadio\tref: refs/heads/master\r\n'
                                       'B\tOther\tabc\nG 5 100 200\r\n')
            self.assertEqual(w['procs'], [('RoRadio', 'dotnet.exe', '2026-01-01T10:00:00+02:00')])
            self.assertEqual(w['branches'], {'RoRadio': 'master', 'Other': 'detached abc'})
            self.assertEqual(w['gpu']['util_pct'], 5.0)
            looks = projects.probe_all(cfg, activity_fn=lambda m: {'procs': [], 'repos': {}, 'gpu': None},
                                       windows_fn=lambda m, f: w)
            self.assertEqual(looks['pc']['gpu']['util_pct'], 5.0)
            self.assertNotIn('windows', looks['nuc'])

    def test_windows_over_ssh_failing_says_so(self):
        with mock.patch('subprocess.run', side_effect=OSError('no ssh')):
            self.assertIn('not looked at', projects.probe_windows({'host': 'pc.example'}, [('a', 'b')])['error'])
        with mock.patch('subprocess.run', return_value=subprocess.CompletedProcess([], 255, '', 'refused')):
            self.assertIn('exit 255', projects.probe_windows({'host': 'pc.example'}, [('a', 'b')])['error'])
        with mock.patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'B\tRoRadio\tref: refs/heads/dev\n', '')):
            self.assertEqual(projects.probe_windows({'host': 'pc.example'}, [('RoRadio', 'C:\\r')])['branches'], {'RoRadio': 'dev'})


class ProjectsStateTest(unittest.TestCase):
    def test_counts_per_project_with_machines_flags_and_gpu(self):
        with tempfile.TemporaryDirectory() as d:
            repo(d, worktree=os.path.join(d, 'wt', 'pwa'))
            cfg = cfg_of(d)
            jobs = [{'kind': 'agent', 'project': 'the-map', 'status': 'running', 'updated': None, 'started': '2026-01-01T00:00:00+00:00',
                     'repo': '/r', 'branch': 'b'},
                    {'kind': 'run', 'project': 'the-map', 'status': 'done', 'updated': '2026-01-01T01:00:00+00:00'},
                    {'kind': 'quiet', 'project': None, 'status': 'done'}]
            activity = {
                'nuc': {'procs': [('/home/u/proj-wt/pwa', 'node'), ('/home/u/proj-wt/pwa', 'bash'), ('/home/u/x', 'claude')],
                        'repos': {'/home/u/proj-wt/pwa': {'top': '/home/u/proj-wt/pwa', 'branch': 'detached 1', 'remote': '',
                                                          'repo': '/home/u/proj-wt/pwa'},
                                  '/home/u/x': {'top': '/home/u/x', 'branch': 'main', 'remote': 'https://e/Tool.git', 'repo': '/home/u/x'}},
                        'gpu': None},
                'pc': {'procs': [], 'repos': {}, 'gpu': {'util_pct': 80.0, 'mem_used_mib': 2048.0, 'mem_total_mib': 32768.0},
                       'windows': {'procs': [('RoRadio', 'dotnet.exe', '2026-01-01T02:00:00+00:00'), ('RoRadio', 'cmd.exe', 'x')],
                                   'branches': {'RoRadio': 'master'}, 'gpu': None}},
                'gone': {'error': 'did not answer'},
                'win': {'procs': [], 'repos': {}, 'gpu': None, 'windows': {'error': 'exit 255'}}}
            machines = [{'name': 'pc', 'paused': True, 'status': 'paused'}]
            run = {'status': 'running', 'cwd': os.path.join(d, 'wt', 'pwa'), 'agents': [{'agent': 'agy'}]}
            out = projects.projects_state(cfg, jobs, machines, activity, run, '2026-01-01T03:00:00+00:00')
            by = {p['name']: p for p in out['items']}
            self.assertEqual(out['errors'], {'gone': 'did not answer', 'win (Windows)': 'exit 255'})
            m = by['the-map']
            self.assertEqual((m['jobs'], m['running'], m['agents'], m['runs'], m['conductor']), (2, 1, 1, 1, 'agy'))
            self.assertEqual(m['machines'][0], {'name': 'nuc', 'processes': 1, 'agents': 0, 'programs': ['node']})
            self.assertEqual(by['Tool']['machines'][0]['agents'], 1)
            ro = by['RoRadio']
            self.assertEqual(ro['repos'], [{'machine': 'pc', 'path': 'C:\\me\\RoRadio', 'branch': 'master'}])
            pc = ro['machines'][0]
            self.assertEqual((pc['processes'], pc['programs'], pc['yield_flag'], pc['gpu']['util_pct']), (1, ['dotnet.exe'], True, 80.0))
            self.assertEqual(ro['last_activity'], '2026-01-01T02:00:00+00:00')

    def test_nothing_seen_is_an_empty_list(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_of(d, projects={})
            self.assertEqual(projects.projects_state(cfg, [], [], None), {'items': [], 'errors': {}})


class ConfigTest(unittest.TestCase):
    def test_projects_errors_name_the_key(self):
        cfg = config.normalise({'machines': {'nuc': {'host': 'nuc.example'}},
                                'projects': {'a': {'match': '(', 'machines': ['zz'], 'windows': {'nuc': 'C:\\a'}}, 'b': 'x'}})
        errs = config.validate(cfg)
        for part in ('projects.a.match', 'projects.a.machines names "zz"', 'projects.a.windows names "nuc"', 'projects.b is'):
            self.assertTrue(any(part in e for e in errs), (part, errs))
        self.assertIn('projects is', config.validate(config.normalise({'projects': [1]}))[-1])


if __name__ == '__main__':
    unittest.main()
