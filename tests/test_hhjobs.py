"""Compute on another member's machine with no login there (docs/HOUSEHOLD.md, *Compute on shared machines*): Bo's test
travels as a WIP ref and a signed job, Ana's Tutti (pc's owner) runs it in a sandboxed worktree and returns the exit code
and output through the coordinator; nothing of Bo's but the repo URL, the ref and the command reaches Ana's side. Temp
folders, a bare origin repository and the coordinator's service in this process."""
import io
import os
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from test_household import DirectClient, Household, as_user

from tutti import config, coordinator, hhjobs, household, routing, users


def sh(*argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ComputeTest(unittest.TestCase):
    def setUp(self):
        self.h = Household(self)
        self.addCleanup(self.h.close)
        with as_user(self.h.bo_home):
            coordinator.join(self.h.bo, self.h.code, 'Bo', 'cursor', clock=self.h.clock)
        self.ana = DirectClient(self.h.service, self.h.membership(self.h.ana_home), self.h.clock)
        self.bo = DirectClient(self.h.service, self.h.membership(self.h.bo_home), self.h.clock)
        # Ana's pc, shared; a container runner (the default sandbox for members' runs)
        self.h.ana['machines']['pc'] = config.machine_of(self.h.ana, 'pc', {'host': 'pc.lan', 'slots': 2,
                                                                            'runner': {'kind': 'docker', 'image': 'img'}})
        self.ana.call('/hh/machine', {'action': 'share', 'name': 'pc', 'info': {'slots': 2}})
        with as_user(self.h.ana_home):
            users.write(config.state_dir(self.h.ana), machine_overrides={'pc': {'household_shared': True}})
        # one origin both can read; Bo's work tree has an uncommitted change
        # the shared project's origin: an https URL git maps to a bare folder here (file:// allowed for this test only)
        self.origin = os.path.join(tempfile.mkdtemp(), 'app.git')
        sh('git', 'init', '-q', '--bare', self.origin)
        url = 'https://git.example/home/app.git'
        gitconfig = os.path.join(tempfile.mkdtemp(), 'gitconfig')
        with open(gitconfig, 'w') as f:
            f.write(f'[url "{self.origin}"]\n\tinsteadOf = {url}\n')
        for patcher in (mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': gitconfig}),
                        mock.patch.dict(hhjobs.SAFE_GIT, {'GIT_ALLOW_PROTOCOL': 'https:ssh:file'})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.src = self.h.bo['autopilot']['repos']['app']['path']
        sh('git', '-C', self.src, 'remote', 'set-url', 'origin', url)
        self.bo.call('/hh/sync', {'beat': {}, 'projects': {'git.example/home/app': {'publish': []}}})   # a shared project
        for k, v in (('user.email', 'bo@example.com'), ('user.name', 'Bo')):
            sh('git', '-C', self.src, 'config', k, v)
        with open(os.path.join(self.src, 'check.sh'), 'w') as f:
            f.write('grep -q ready flag.txt && echo "all tests passed"\n')
        with open(os.path.join(self.src, 'flag.txt'), 'w') as f:
            f.write('not yet\n')
        sh('git', '-C', self.src, 'add', '.')
        sh('git', '-C', self.src, 'commit', '-qm', 'base')
        with open(os.path.join(self.src, 'flag.txt'), 'w') as f:
            f.write('ready\n')   # uncommitted: the WIP ref carries it
        self.seen_jobs = []

    def exec_in_sandbox(self, argv, **kw):
        """What Ana's `tutti test --dir <sandbox> --on pc -- <cmd>` does, minus ssh and the container: run it in the sandbox."""
        self.assertEqual(kw['env'].get(hhjobs.JOB_ENV) is not None, True)
        tree, cmd = argv[argv.index('--dir') + 1], argv[argv.index('--') + 1:]
        self.assertEqual(argv[argv.index('--on') + 1], 'pc')
        return subprocess.run(cmd, cwd=tree, capture_output=True, text=True)

    def owner_runs(self, stop):
        """Ana's dashboard rounds: claim Bo's jobs for pc and run each (in this process instead of a background tutti)."""
        def spawn(argv, **kw):
            jid = argv[-1]
            self.seen_jobs.append(jid)   # no environment patching in this thread: Ana's state folder and client are explicit
            hhjobs.run_job(self.h.ana, jid, client=self.ana, exec_fn=self.exec_in_sandbox)
            return SimpleNamespace(pid=1)
        while not stop.is_set():
            hhjobs.tick(self.h.ana, client=self.ana, spawn=spawn)
            stop.wait(0.05)

    def bo_runs(self, command):
        stop = threading.Event()
        owner = threading.Thread(target=self.owner_runs, args=(stop,), daemon=True)
        owner.start()
        out = io.StringIO()
        try:
            machine = config.machine_of(self.h.bo, 'pc', {'transport': hhjobs.TRANSPORT, 'slots': 2})
            with as_user(self.h.bo_home):
                rc = hhjobs.run_on(self.h.bo, machine, self.src, command, out=out, client=self.bo, sleep=lambda s: None)
        finally:
            stop.set()
            owner.join(5)
        return rc, out.getvalue()

    def test_bos_test_runs_on_anas_machine_with_his_uncommitted_change(self):
        rc, text = self.bo_runs(['bash', 'check.sh'])
        self.assertEqual(rc, 0, text)
        self.assertIn('all tests passed', text)
        self.assertEqual(sh('git', '--git-dir', self.origin, 'for-each-ref', 'refs/tutti-wip'), '')   # the WIP ref is gone
        self.assertEqual(sh('git', '-C', self.src, 'status', '--short'), 'M flag.txt')                 # his tree untouched

    def test_a_failing_run_returns_its_exit_code_and_output(self):
        rc, text = self.bo_runs(['bash', '-c', 'echo boom; exit 3'])
        self.assertEqual(rc, 3)
        self.assertIn('boom', text)

    def test_the_job_carries_no_credential_only_repo_ref_and_command(self):
        self.bo_runs(['bash', 'check.sh'])
        data = coordinator.load(coordinator.state_path(self.h.ana))
        job = data['jobs'][self.seen_jobs[0]]
        self.assertEqual(sorted(job), sorted(['by', 'machine', 'repo', 'ref', 'command', 'device', 'timeout', 'kind', 'state', 'at',
                                              'claimed', 'rc', 'tail', 'finished']))
        self.assertTrue(job['ref'].startswith('refs/tutti-wip/'))
        self.assertNotIn(self.h.membership(self.h.bo_home)['token'], str(data['jobs']))

    def test_a_shell_runner_needs_the_owners_opt_in(self):
        self.h.ana['machines']['pc']['runner']['kind'] = 'shell'
        rc, text = self.bo_runs(['bash', 'check.sh'])
        self.assertEqual(rc, 125)
        self.assertIn('member_shell', text)
        self.h.ana['household']['member_shell'] = True
        self.assertEqual(self.bo_runs(['bash', 'check.sh'])[0], 0)

    def test_only_the_owner_claims_and_no_one_reads_anothers_job(self):
        jid = self.bo.call('/hh/job', {'action': 'submit', 'job': {'machine': 'pc', 'repo': 'https://git.example/home/app.git',
                                                                   'ref': 'refs/tutti-wip/x/1', 'command': ['true']}})['id']
        self.assertEqual(self.bo.call('/hh/job', {'action': 'claim'})['jobs'], [])
        with self.assertRaisesRegex(household.HouseholdError, 'of yours'):
            self.ana.call('/hh/job', {'action': 'status', 'id': jid})
        with self.assertRaisesRegex(household.HouseholdError, 'refs/tutti-wip'):
            self.bo.call('/hh/job', {'action': 'submit', 'job': {'machine': 'pc', 'repo': 'r', 'ref': 'refs/heads/main',
                                                                 'command': ['true']}})

    def test_only_a_shared_projects_https_or_ssh_remote_may_be_fetched(self):
        for repo in ('https://evil.example/x.git', '/home/ana/.ssh', 'file:///etc', 'ext::sh -c id'):
            with self.assertRaisesRegex(household.HouseholdError, 'shared projects'):
                self.bo.call('/hh/job', {'action': 'submit', 'job': {'machine': 'pc', 'repo': repo, 'ref': 'refs/tutti-wip/x/1',
                                                                     'command': ['true']}})

    def test_a_paused_machine_takes_no_more_queued_runs_and_a_live_claim_is_not_taken_twice(self):
        job = {'machine': 'pc', 'repo': 'https://git.example/home/app.git', 'ref': 'refs/tutti-wip/x/1', 'command': ['true'],
               'timeout': 3600}
        self.bo.call('/hh/job', {'action': 'submit', 'job': job})
        self.assertEqual(len(self.ana.call('/hh/job', {'action': 'claim'})['jobs']), 1)
        self.h.clock.t += 3600   # still within its timeout plus the grace: not claimed again
        self.assertEqual(self.ana.call('/hh/job', {'action': 'claim'})['jobs'], [])
        self.bo.call('/hh/job', {'action': 'submit', 'job': job})
        self.ana.call('/hh/machine', {'action': 'pause', 'name': 'pc'})
        self.assertEqual(self.ana.call('/hh/job', {'action': 'claim'})['jobs'], [])

    def test_a_result_the_coordinator_missed_is_sent_again(self):
        self.bo.call('/hh/job', {'action': 'submit', 'job': {'machine': 'pc', 'repo': 'https://git.example/home/app.git',
                                                             'ref': 'refs/tutti-wip/x/1', 'command': ['true']}})
        j = self.ana.call('/hh/job', {'action': 'claim'})['jobs'][0]
        os.makedirs(hhjobs.folder(self.h.ana), exist_ok=True)
        import json
        with open(os.path.join(hhjobs.folder(self.h.ana), f'{j["id"]}.json'), 'w') as f:
            json.dump(j, f)
        self.ana.down = True
        hhjobs.run_job(self.h.ana, j['id'], client=self.ana, exec_fn=self.exec_in_sandbox)   # the fetch fails: rc 125, kept
        self.assertTrue(os.path.exists(os.path.join(hhjobs.folder(self.h.ana), f'{j["id"]}.result')))
        self.ana.down = False
        hhjobs.tick(self.h.ana, client=self.ana, spawn=lambda *a, **k: None)
        self.assertFalse(os.path.exists(os.path.join(hhjobs.folder(self.h.ana), f'{j["id"]}.result')))
        self.assertEqual(self.bo.call('/hh/job', {'action': 'status', 'id': j['id']})['state'], 'done')

    def test_a_repo_without_origin_says_what_to_do(self):
        sh('git', '-C', self.src, 'remote', 'remove', 'origin')
        rc, text = self.bo_runs(['true'])
        self.assertEqual(rc, 3)
        self.assertIn('no origin remote', text)

    def test_a_household_machine_is_never_reached_over_ssh(self):
        from tutti import remote
        machine = config.machine_of(self.h.bo, 'pc', {'transport': hhjobs.TRANSPORT, 'slots': 2})
        self.assertEqual(remote.sh(machine, 'id').returncode, 1)
        self.assertIsInstance(remote.probe(machine), routing.Probe)
        self.assertEqual(config.validate({**config.normalise({'machines': {'pc': {'transport': 'household'}}}), '_path': 'x'}), [])

    # ---- security audit 2026-11 (docs/SECURITY-AUDIT-2026-11.md, C1 and C2)

    def bo_runs_on_device(self, device):
        stop = threading.Event()
        owner = threading.Thread(target=self.owner_runs, args=(stop,), daemon=True)
        owner.start()
        out = io.StringIO()
        try:
            machine = config.machine_of(self.h.bo, 'pc', {'transport': hhjobs.TRANSPORT, 'slots': 2})
            with as_user(self.h.bo_home):
                rc = hhjobs.run_on(self.h.bo, machine, self.src, ['id'], device=device, out=out, client=self.bo,
                                   sleep=lambda s: None)
        finally:
            stop.set()
            owner.join(5)
        return rc, out.getvalue()

    def test_a_device_never_takes_a_members_run_out_of_the_container(self):
        # exploit: the job names a device whose runner is a shell (Gradle on the host) on another of the owner's machines
        self.h.ana['machines']['mini'] = config.machine_of(self.h.ana, 'mini', {'host': 'mini.lan', 'slots': 1})
        self.h.ana['devices'] = {'phone': {'name': 'phone', 'machine': 'mini', 'serial': 'S1', 'runner': {'kind': 'shell'}},
                                 'tab': {'name': 'tab', 'machine': 'pc', 'serial': 'S2', 'runner': {'kind': 'shell'}}}
        for device, why in (('any', 'never "any"'), ('phone', 'not on the shared machine'), ('tab', 'member_shell'),
                            ('ghost', 'named in its owner')):
            rc, text = self.bo_runs_on_device(device)
            self.assertEqual(rc, 125, text)
            self.assertIn(why, text)

    def test_the_run_never_sees_the_owners_git_cache(self):
        # exploit: a linked worktree mounted the cache read-write; a hook planted there ran under the owner's git
        rc, text = self.bo_runs(['bash', '-c', 'test -e .git && echo HAS-GIT; ls -a; true'])
        self.assertEqual(rc, 0, text)
        self.assertNotIn('HAS-GIT', text)
        self.assertIn('flag.txt', text)
        cache = os.path.join(hhjobs.folder(self.h.ana), 'repos', hhjobs.slug('https://git.example/home/app.git'))
        self.assertEqual(os.listdir(os.path.join(cache, 'worktrees')) if os.path.isdir(os.path.join(cache, 'worktrees')) else [], [])
        self.assertEqual([d for d in os.listdir(hhjobs.folder(self.h.ana)) if d.startswith('j') and os.path.isdir(
            os.path.join(hhjobs.folder(self.h.ana), d))], [])   # the tree is gone after the run

    def test_a_claimed_job_whose_id_is_a_path_is_never_written(self):
        class Fake:
            def call(self, path, body):
                return {'jobs': [{'id': '../../../evil'}, {'id': 'j7', 'machine': 'pc'}]} if body.get('action') == 'claim' else {}
        started = hhjobs.tick(self.h.ana, client=Fake(), spawn=lambda *a, **k: None)
        self.assertEqual(started, ['j7'])

    def test_an_archive_with_a_link_out_of_the_tree_is_refused(self):
        import tarfile
        d = tempfile.mkdtemp()
        import shutil
        self.addCleanup(shutil.rmtree, d, True)
        tb = os.path.join(d, 'x.tar')
        with tarfile.open(tb, 'w') as t:
            info = tarfile.TarInfo('evil')
            info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
            t.addfile(info)
            info2 = tarfile.TarInfo('evil/x')
            t.addfile(info2, io.BytesIO(b''))
        with self.assertRaises(hhjobs.JobError):
            hhjobs.extract(tb, os.path.join(d, 'tree'))
        self.assertFalse(os.path.exists('/etc/passwd/x'))


if __name__ == '__main__':
    unittest.main()
