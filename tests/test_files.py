"""Files for other machines: the secret check, manifests, the cache's budget, progress, and real sends over the `local`
transport into temp folders (rsync and the stream fallback, resume, skip, damage, clean) plus the CLI and the dashboard."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tutti import cli, config, files, routing, state, transfer
from tutti.files import Entry

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GB = files.GB


def write(path, data=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def cfg_in(folder, budget_gb=1, **extra):
    raw = {'project': {'worktrees': folder, 'state': os.path.join(folder, 'state')},
           'machines': {'here': {'host': 'local', 'files': {'cache': os.path.join(folder, 'cache'), 'budget_gb': budget_gb,
                                                            'keep_free_gb': 0}}}, **extra}
    cfg = config.normalise(raw)
    cfg['_path'] = os.path.join(folder, 'tutti.config.yaml')
    return cfg


class SecretTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_secret_names_folders_and_key_json_are_refused(self):
        for name in ('.env', '.env.local', '.envrc', '.env-production',
                     'github-token.txt', 'my-service-account.json', 'id_ed25519', 'server.pem'):
            self.assertIsNotNone(files.secret_reason(write(os.path.join(self.d, name))), name)
        self.assertIn('.ssh/', files.secret_reason(write(os.path.join(self.d, '.ssh', 'config'))))
        key = write(os.path.join(self.d, 'project-1234.json'), b'{"type": "service_account", "private_key": "-----"}')
        self.assertIn('private key', files.secret_reason(key))

    def test_ordinary_files_pass_and_extra_patterns_apply(self):
        self.assertIsNone(files.secret_reason(write(os.path.join(self.d, 'harti.zip'))))
        self.assertIsNone(files.secret_reason(write(os.path.join(self.d, 'data.json'), b'{"a": 1}')))
        self.assertIsNotNone(files.secret_reason(write(os.path.join(self.d, 'x.sqlite')), ['*.sqlite']))

    def test_a_symlink_to_a_secret_is_refused(self):
        key = write(os.path.join(self.d, '.ssh', 'id_ed25519'), b'k')
        os.symlink(key, os.path.join(self.d, 'notes.txt'))
        self.assertIsNotNone(files.secret_reason(os.path.join(self.d, 'notes.txt')))

    def test_a_secret_inside_a_folder_is_found(self):
        write(os.path.join(self.d, 'set', 'a.csv'))
        write(os.path.join(self.d, 'set', 'sub', '.env'))
        found = files.secrets_in([os.path.join(self.d, 'set')])
        self.assertEqual([os.path.basename(p) for p, _ in found], ['.env'])


class PureTest(unittest.TestCase):
    def test_eviction_takes_least_recently_used_first_and_keeps_what_is_sent(self):
        entries = [Entry('old', 4, 1), Entry('mid', 4, 2), Entry('new', 4, 3)]
        self.assertEqual(files.plan_eviction(entries, 10, 5), (['old', 'mid'], True))
        self.assertEqual(files.plan_eviction(entries, 10, 7, keep=['old']), (['mid', 'new'], False))
        self.assertEqual(files.plan_eviction(entries, 20, 5), ([], True))
        self.assertEqual(files.plan_eviction([], 10, 11), ([], False))

    def test_rsync_progress_and_progress_line(self):
        self.assertEqual(files.rsync_progress('\r     32,768   0%  0.00kB/s\r  1,048,576  50%   10.00MB/s    0:00:01'), 1048576)
        self.assertIsNone(files.rsync_progress('sending incremental file list'))
        self.assertEqual(files.progress_line(512 * files.MB, 1024 * files.MB), '512 1024 MB\n')
        self.assertEqual(files.progress_line(0, 10), '0 1 MB\n')
        self.assertEqual(files.progress_line(10, 10), '1 1 MB\n')

    def test_extras_and_pins(self):
        item = files.Item('set', '/x/set', True, [('set/a', 1, 'h')], 1, ['set'])
        self.assertEqual(files.extras(['set/a', 'set/old', 'set/b' + files.PART_SUFFIX, f'set/{files.PARTIAL}/c', ''], item),
                         ['set/old'])
        self.assertEqual(files.parse_pins('PIN\ta.zip\nITEM\tx\nPIN\tb'), {'a.zip', 'b'})
        self.assertEqual(files.parse_number('FREE=12\nPARTIAL=x', 'FREE'), 12)
        self.assertIsNone(files.parse_number('PARTIAL=x', 'PARTIAL'))

    def test_listing_and_names(self):
        text = 'RSYNC=1\nITEM\ta.zip\t100\t{"bytes": 5}\nITEM\tbroken\t1\tnot json\nITEM\tshort\n'
        self.assertEqual(list(files.parse_listing(text)), ['a.zip'])
        self.assertEqual(files.parse_listing(text)['a.zip'][0], Entry('a.zip', 5, 100.0))
        self.assertFalse(any(files.valid_name(n) for n in ('', '.', '..', 'a/b', files.META, 'x' + files.PART_SUFFIX)))

    def test_config_errors_name_the_key(self):
        cfg = config.normalise({'files': {'budget_gb': 0, 'refuse': 'x'}, 'machines': {'m': {'host': 'm.example', 'files': 'no'}}})
        errs = config.validate(cfg)
        self.assertTrue(any('files.budget_gb' in e for e in errs))
        self.assertTrue(any('files.refuse' in e for e in errs))
        self.assertTrue(any('machines.m.files' in e for e in errs))
        self.assertEqual(files.errors(config.normalise({'files': {'cache': '~/c', 'budget_gb': 5, 'bwlimit_kbps': 0}})), [])


class SendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        env = mock.patch.dict(os.environ, {'XDG_STATE_HOME': os.path.join(self.d, 'xdg')})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = cfg_in(self.d)
        self.m = self.cfg['machines']['here']
        self.cache = os.path.join(self.d, 'cache')
        self.src = os.path.join(self.d, 'src')
        self.archive = write(os.path.join(self.src, 'harti.zip'), os.urandom(3 * files.MB + 17))
        write(os.path.join(self.src, 'set', 'a.csv'), b'a,b\n1,2\n')
        write(os.path.join(self.src, 'set', 'deep', 'b.txt'), b'hello')

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, paths, **kw):
        with redirect_stderr(io.StringIO()):
            return transfer.send(self.cfg, self.m, paths, **kw)

    def same(self, rel):
        with open(os.path.join(self.src, rel), 'rb') as a, open(os.path.join(self.cache, rel), 'rb') as b:
            return a.read() == b.read()

    @unittest.skipUnless(shutil.which('rsync'), 'rsync not installed')
    def test_rsync_sends_a_file_and_a_folder_verified_then_skips_them(self):
        base, out = self.send([self.archive, os.path.join(self.src, 'set')], use_rsync=True)
        self.assertEqual(base, self.cache)
        self.assertEqual([(n, how) for n, how, _ in out], [('harti.zip', 'sent'), ('set', 'sent')])
        self.assertTrue(self.same('harti.zip') and self.same('set/deep/b.txt'))
        _, out = self.send([self.archive], use_rsync=True)
        self.assertEqual(out[0][1], 'already there')

    def test_stream_fallback_resumes_a_partial_file(self):
        part = os.path.join(self.cache, 'harti.zip' + files.PART_SUFFIX)
        with open(self.archive, 'rb') as f:
            write(part, f.read(files.MB))
        self.send([self.archive, os.path.join(self.src, 'set')], use_rsync=False)
        self.assertTrue(self.same('harti.zip') and self.same('set/a.csv'))
        self.assertFalse(os.path.exists(part))

    def test_a_damaged_copy_is_sent_again_and_a_changed_file_is_resent(self):
        real_verify, calls = transfer.verify, []

        def flaky(machine, base, item):
            calls.append(item.name)
            return (False, ['harti.zip: FAILED']) if len(calls) == 1 else real_verify(machine, base, item)
        with mock.patch.object(transfer, 'verify', flaky):
            self.send([self.archive], use_rsync=False)
        self.assertEqual(calls, ['harti.zip', 'harti.zip'])
        write(self.archive, b'new content')
        _, out = self.send([self.archive], use_rsync=False)
        self.assertEqual(out[0][1], 'sent')
        self.assertTrue(self.same('harti.zip'))

    def test_damaged_twice_fails_with_the_fix(self):
        with mock.patch.object(transfer, 'verify', lambda *a: (False, [])), self.assertRaisesRegex(RuntimeError, 'damaged.*twice'):
            self.send([self.archive], use_rsync=False)

    def test_secrets_refuse_the_whole_send(self):
        write(os.path.join(self.src, 'set', '.env.local'), b'K=V')
        with self.assertRaisesRegex(RuntimeError, 'refused, nothing was copied.*env.local'):
            self.send([self.archive, os.path.join(self.src, 'set')])
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'harti.zip')))

    def test_missing_path_and_duplicate_names_are_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'not found here'):
            self.send([os.path.join(self.d, 'nope')])
        write(os.path.join(self.d, 'other', 'harti.zip'))
        with self.assertRaisesRegex(RuntimeError, 'both called harti.zip'):
            self.send([self.archive, os.path.join(self.d, 'other', 'harti.zip')])

    def test_budget_removes_the_least_recently_used_and_refuses_what_never_fits(self):
        self.cfg = cfg_in(self.d, budget_gb=4 * files.MB / GB)
        self.m = self.cfg['machines']['here']
        small = write(os.path.join(self.src, 'small.bin'), os.urandom(files.MB))
        self.send([small], use_rsync=False)
        os.utime(os.path.join(self.cache, files.META, 'small.bin.json'), (1, 1))
        self.send([self.archive], use_rsync=False)
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'small.bin')))
        big = write(os.path.join(self.src, 'big.bin'), os.urandom(5 * files.MB))
        with self.assertRaisesRegex(RuntimeError, 'does not fit'):
            self.send([big], use_rsync=False)

    def test_free_space_is_checked_for_a_dest(self):
        self.cfg['machines']['here']['files']['keep_free_gb'] = 10 ** 9
        with self.assertRaisesRegex(RuntimeError, 'free'):
            self.send([self.archive], dest=os.path.join(self.d, 'elsewhere'))

    def test_dest_puts_the_item_there_and_the_job_is_recorded(self):
        dest = os.path.join(self.d, 'elsewhere')
        base, _ = self.send([os.path.join(self.src, 'set')], dest=dest)
        self.assertEqual(base, dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, 'set', 'deep', 'b.txt')))
        jobs = state.jobs_state(self.cfg, 0, is_alive=lambda pid: False)['items']
        self.assertEqual((jobs[0]['kind'], jobs[0]['status']), ('files', 'done'))
        self.assertIn('-> here:', jobs[0]['title'])
        self.assertEqual(jobs[0]['progress']['unit'], 'MB')

    def test_stream_keeps_empty_folders_and_drops_removed_files(self):
        os.makedirs(os.path.join(self.src, 'set', 'empty'))
        self.send([os.path.join(self.src, 'set')], use_rsync=False)
        self.assertTrue(os.path.isdir(os.path.join(self.cache, 'set', 'empty')))
        os.remove(os.path.join(self.src, 'set', 'a.csv'))
        _, out = self.send([os.path.join(self.src, 'set')], use_rsync=False)
        self.assertEqual(out[0][1], 'sent')
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'set', 'a.csv')))
        os.makedirs(os.path.join(self.src, 'blank'))
        self.assertEqual(self.send([os.path.join(self.src, 'blank')])[1][0][1], 'sent')

    def test_a_pinned_item_is_neither_evicted_nor_replaced_nor_cleaned(self):
        self.cfg = cfg_in(self.d, budget_gb=4 * files.MB / GB)
        self.m = self.cfg['machines']['here']
        small = write(os.path.join(self.src, 'small.bin'), os.urandom(files.MB))
        self.send([small], use_rsync=False, pin_token='job1')
        with self.assertRaisesRegex(RuntimeError, 'does not fit'):
            self.send([self.archive], use_rsync=False)
        write(small, b'changed')
        with self.assertRaisesRegex(RuntimeError, 'input of a running job'):
            self.send([small], use_rsync=False)
        with self.assertRaisesRegex(RuntimeError, 'input of a running job'):
            transfer.clean(self.cfg, self.m, ['small.bin'])
        self.assertTrue(transfer.stored(self.cfg, self.m)['items'][0]['pinned'])
        transfer.unpin(self.m, self.cache, ['small.bin'], 'job1')
        self.assertEqual(self.send([small], use_rsync=False)[1][0][1], 'sent')

    def test_a_replaced_item_counts_once_and_partials_count_and_are_cleaned(self):
        self.cfg = cfg_in(self.d, budget_gb=4 * files.MB / GB)
        self.m = self.cfg['machines']['here']
        self.send([self.archive], use_rsync=False)
        write(self.archive, os.urandom(3 * files.MB))
        self.assertEqual(self.send([self.archive], use_rsync=False)[1][0][1], 'sent')
        part = write(os.path.join(self.cache, 'other.bin' + files.PART_SUFFIX), os.urandom(2 * files.MB))
        self.assertEqual(transfer.stored(self.cfg, self.m)['partial_bytes'], 2 * files.MB)
        write(self.archive, os.urandom(3 * files.MB))
        with self.assertRaisesRegex(RuntimeError, 'does not fit'):
            self.send([self.archive], use_rsync=False)
        os.utime(part, (1, 1))
        transfer.clean(self.cfg, self.m)
        self.assertFalse(os.path.exists(part))

    def test_an_interrupted_update_does_not_pass_for_the_old_version(self):
        self.send([self.archive], use_rsync=False)
        with open(self.archive, 'rb') as f:
            old = f.read()
        write(self.archive, b'new')
        with mock.patch.object(transfer, 'copy_stream', side_effect=RuntimeError('cut')), self.assertRaises(RuntimeError):
            self.send([self.archive], use_rsync=False)
        write(self.archive, old)
        self.assertEqual(self.send([self.archive], use_rsync=False)[1][0][1], 'sent')

    def test_progress_marks_the_log_active(self):
        job = transfer.Job(self.cfg, 't')
        os.utime(job.log_path, (1, 1))
        job.total = 10
        job.progress(5)
        self.assertGreater(os.path.getmtime(job.log_path), 1)
        job.end(0)

    def test_stored_and_clean(self):
        self.send([self.archive, os.path.join(self.src, 'set')], use_rsync=False)
        st = transfer.stored(self.cfg, self.m)
        self.assertEqual(sorted(i['name'] for i in st['items']), ['harti.zip', 'set'])
        self.assertEqual(st['bytes'], os.path.getsize(self.archive) + 8 + 5)
        self.assertEqual(transfer.clean(self.cfg, self.m), [])
        self.assertEqual(transfer.clean(self.cfg, self.m, ['set']), ['set'])
        with self.assertRaisesRegex(RuntimeError, 'has no set'):
            transfer.clean(self.cfg, self.m, ['set'])
        self.assertEqual(transfer.clean(self.cfg, self.m, everything=True), ['harti.zip'])
        self.assertEqual(os.listdir(self.cache), [files.META])

    def test_stored_reports_a_problem_instead_of_raising(self):
        self.cfg['machines']['here']['files']['cache'] = write(os.path.join(self.d, 'a-file'))
        self.assertIn('could not create or open', transfer.stored(self.cfg, self.m)['problem'])


class PathsTest(unittest.TestCase):
    def test_rsync_targets_and_shell_paths(self):
        ssh = {'transport': 'ssh', 'host': 'box.example'}
        self.assertEqual(transfer.rsync_target(ssh, '~/.cache/tutti/files'), 'box.example:.cache/tutti/files/')
        self.assertEqual(transfer.rsync_target(ssh, '/data/in'), 'box.example:/data/in/')
        self.assertEqual(transfer.shell_path('~/c', 'a b'), '"$HOME"/c/\'a b\'')

    def test_inputs_reach_the_command_and_the_container(self):
        from tutti import remote
        export, mount = remote.inputs_env('"$HOME"/c')
        self.assertEqual(export, 'export TUTTI_INPUTS="$HOME"/c && ')
        self.assertIn('-v "$TUTTI_INPUTS:$TUTTI_INPUTS:ro"', mount)
        self.assertEqual(remote.inputs_env(None), ('', ''))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        env = mock.patch.dict(os.environ, {'XDG_STATE_HOME': os.path.join(self.d, 'xdg')})
        env.start()
        self.addCleanup(env.stop)
        cfg = cfg_in(self.d)
        raw = {'project': cfg['project'], 'machines': {'here': {'host': 'local', 'files': cfg['machines']['here']['files']}}}
        self.path = os.path.join(self.d, 'tutti.config.json')
        with open(self.path, 'w', encoding='utf8') as f:
            json.dump(raw, f)
        self.src = write(os.path.join(self.d, 'in', 'data.bin'), b'1234')

    def tearDown(self):
        self.tmp.cleanup()

    def tutti(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(['--config', self.path, *args])
        return rc, out.getvalue(), err.getvalue()

    def test_send_list_clean(self):
        rc, out, _ = self.tutti('files', 'send', self.src, '--to', 'here')
        self.assertEqual((rc, 'here: data.bin sent' in out), (0, True))
        rc, out, _ = self.tutti('files', 'list', '--on', 'here')
        self.assertIn('data.bin', out)
        rc, out, _ = self.tutti('files', 'clean', '--on', 'here', 'data.bin')
        self.assertIn('removed data.bin', out)
        self.tutti('files', 'send', self.src, '--to', 'here')
        rc, out, _ = self.tutti('files', 'clean', '--on', 'here', '--all')
        self.assertIn('removed data.bin', out)

    def test_bad_calls_say_what_to_write(self):
        for args in (('files', 'send', self.src), ('files', 'clean')):
            with self.assertRaises(SystemExit) as e, redirect_stderr(io.StringIO()):
                cli.main(['--config', self.path, *args])
            self.assertEqual(e.exception.code, 2)
        rc, _, err = self.tutti('files', 'send', self.src, '--to', 'all')
        self.assertEqual(rc, 1)
        self.assertIn('no machine other than this computer', err)
        rc, _, err = self.tutti('files', 'send', write(os.path.join(self.d, 'in', 'api-token')), '--to', 'here')
        self.assertEqual(rc, 1)
        self.assertIn('Secrets never leave', err)

    def test_test_input_is_copied_first_and_named_in_tutti_inputs(self):
        tree = os.path.join(self.d, 'track')
        os.makedirs(tree)
        rc, _, err = self.tutti('test', '--on', 'here', '--input', self.src, '--dir', tree, 'track',
                                'test -f "$TUTTI_INPUTS/data.bin"')
        self.assertEqual(rc, 0, err)
        self.assertIn('inputs on here: data.bin (sent)', err)
        self.assertFalse([f for f in os.listdir(os.path.join(self.d, 'cache', files.META)) if '.pin-' in f])

    def test_run_input_needs_on(self):
        with open(self.path, encoding='utf8') as f:
            raw = json.load(f)
        raw['workers'] = {'w': {'kind': 'cli', 'command': ['true', '{prompt}']}}
        with open(self.path, 'w', encoding='utf8') as f:
            json.dump(raw, f)
        rc, _, err = self.tutti('run', 'w', 'task', '--input', self.src)
        self.assertEqual(rc, 2)
        self.assertIn('--on <machine>', err)


class DashboardTest(unittest.TestCase):
    def test_reachable_machines_list_their_stored_files(self):
        cfg = config.normalise({'machines': {'a': {'host': 'a.example'}, 'b': {'host': 'b.example'}}})
        probes = {'a': routing.Probe(True, 1, 0.1, 4, False), 'b': routing.Probe()}
        ms = state.machines_state(cfg, lambda m: probes[m['name']], lambda m: {'items': [], 'name': m['name']})
        self.assertEqual([m['stored'] and m['stored']['name'] for m in ms], ['a', None])

    def test_a_fresh_progress_file_keeps_a_job_from_stalling(self):
        with tempfile.TemporaryDirectory() as d:
            log = write(os.path.join(d, '100-999999.log'), b'copying\n')
            os.utime(log, (1, 1))
            write(log + '.progress', b'1 10 MB\n')
            job = state.log_job(log, 'files', os.path.getmtime(log + '.progress') + 5, 60, lambda pid: True)
            self.assertEqual(job['status'], 'running')

    def test_progress_file_unit(self):
        with tempfile.TemporaryDirectory() as d:
            p = write(os.path.join(d, 'x.progress'), b'5 10 MB\n')
            self.assertEqual(state.read_progress_file(p)['unit'], 'MB')
            write(p, b'5 10\n')
            self.assertNotIn('unit', state.read_progress_file(p))


class WindowsImportTest(unittest.TestCase):
    def test_the_cli_imports_where_there_is_no_fcntl(self):
        # Windows has no fcntl: an unguarded import broke every command there (the PC install, 2026-11-14)
        code = 'import sys; sys.modules["fcntl"] = None; import tutti.cli; print("ok")'
        r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, cwd=ROOT_DIR)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, 'ok'), r.stderr[-800:])

    def test_without_flock_the_lock_waits_on_msvcrt_until_it_is_free(self):
        tries = []

        def locking(fd, mode, n):
            tries.append(mode)
            if len(tries) < 3:
                raise OSError('held by another sender')
        fake = mock.Mock(LK_NBLCK=2, locking=locking)
        slept = []
        with mock.patch.object(transfer, 'fcntl', None), mock.patch.dict(sys.modules, {'msvcrt': fake}), \
                tempfile.TemporaryFile() as f:
            transfer.lock_file(f, sleep=slept.append)
        self.assertEqual((tries, slept), ([2, 2, 2], [1, 1]))


if __name__ == '__main__':
    unittest.main()
