"""The household (docs/HOUSEHOLD.md), increment 2: the join code and the token derived on both sides, signed requests and
their refusals, leases, shared backlogs with their dedupe, the coordinator over real HTTP on 127.0.0.1, and two simulated
users (Ana and Bo) on one shared project whose autopilots never do the same task. Temp folders and loopback only."""
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from tutti import autopilot, backlog, config, coordinator, household, householdcmd, sharing, users

REMOTE = 'git@github.com:octo/app.git'


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@contextmanager
def as_user(home):
    """The membership file of one login: XDG_CONFIG_HOME of that user's home."""
    with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': os.path.join(home, 'xdg')}):
        yield


def make_repo(home):
    path = os.path.join(home, 'app')
    subprocess.run(['git', 'init', '-q', path], check=True)
    subprocess.run(['git', '-C', path, 'remote', 'add', 'origin', REMOTE], check=True)
    return path


def make_cfg(home, name, port=0, shared=True):
    raw = {'project': {'state': os.path.join(home, 'state')}, 'user': {'name': name},
           'workers': {'agy': {'kind': 'cli', 'command': ['agy']}}, 'accounts': {'agy-claude': {'worker': 'agy'}},
           'household': {'port': port, 'bind': '127.0.0.1'},
           'autopilot': {'accounts': ['agy-claude'], 'max_parallel': 2,
                         'repos': {'app': {'path': make_repo(home), 'check': 'true', 'household': shared}}}}
    cfg = config.normalise(raw)
    cfg['_path'] = os.path.join(home, 'config.yaml')
    return cfg


class DirectClient:
    """A member's client that calls the coordinator's Service in this process, signed exactly as over HTTP."""

    def __init__(self, service, membership, clock):
        self.service, self.m, self.clock = service, membership, clock
        self.down = False

    def call(self, path, body=None):
        if self.down:
            raise coordinator.Unreachable('the household coordinator at test did not answer (timeout)')
        import json
        raw = json.dumps(body or {}).encode()
        ts, nonce = str(int(self.clock())), os.urandom(8).hex()
        headers = {'X-Tutti-User': self.m['user_id'], 'X-Tutti-Time': ts, 'X-Tutti-Nonce': nonce,
                   'X-Tutti-Sign': household.signature(self.m['token'], 'POST', path, ts, nonce, raw)}
        code, answer = self.service.handle(path, headers, raw, '127.0.0.1')
        if code != 200:
            raise household.HouseholdError(answer['error'])
        return answer


class CodeAndTokenTest(unittest.TestCase):
    def test_the_code_round_trips_and_a_bad_one_names_the_fix(self):
        secret = household.new_secret()
        code = household.make_code('mini.lan', 4778, secret)
        self.assertEqual(household.parse_code(code), ('mini.lan', 4778, secret))
        self.assertEqual(household.parse_code(code.lower().replace('tutti-hh', 'tutti-hh')[:9] + code[9:]), ('mini.lan', 4778, secret))
        for bad in ('', 'hello', 'tutti-hh:mini.lan:99999:' + secret, code[:-3]):
            with self.assertRaises(household.HouseholdError):
                household.parse_code(bad)

    def test_both_sides_derive_the_same_token_and_another_secret_does_not(self):
        a = household.derive_token('S' * 24, 'u1', 'n1', 'n2')
        self.assertEqual(a, household.derive_token('S' * 24, 'u1', 'n1', 'n2'))
        self.assertNotEqual(a, household.derive_token('T' * 24, 'u1', 'n1', 'n2'))

    def test_a_signed_request_passes_once_and_stale_forged_or_replayed_ones_do_not(self):
        seen, now = {}, 1000
        sign = household.signature('k', 'POST', '/hh/sync', now, 'n1', b'{}')
        self.assertIsNone(household.check_signed('k', 'POST', '/hh/sync', now, 'n1', b'{}', sign, now, seen))
        self.assertIn('repeated', household.check_signed('k', 'POST', '/hh/sync', now, 'n1', b'{}', sign, now, seen))
        late = household.signature('k', 'POST', '/hh/sync', now, 'n2', b'{}')
        self.assertIn('off', household.check_signed('k', 'POST', '/hh/sync', now, 'n2', b'{}', late, now + 500, seen))
        self.assertIn('not signed', household.check_signed('x', 'POST', '/hh/sync', now, 'n3', b'{}',
                                                            household.signature('k', 'POST', '/hh/sync', now, 'n3', b'{"a":1}'),
                                                            now, seen))


class KeysTest(unittest.TestCase):
    def test_project_keys_match_whatever_the_remote_form(self):
        for r in ('git@github.com:Octo/App.git', 'https://github.com/octo/app', 'https://github.com/octo/app.git/',
                  'ssh://git@github.com:22/octo/app.git'):
            self.assertEqual(household.project_key(r), 'github.com/octo/app', r)
        self.assertEqual(household.project_key(''), '')

    def test_titles_fold_to_one_key(self):
        self.assertEqual(household.fold('Add unit tests for `tutti/x.py`.'), household.fold('add  UNIT tests for tutti/x py'))


class LeaseTest(unittest.TestCase):
    def test_one_holder_until_it_expires_or_leaves(self):
        s = household.new_state('home', 0)
        self.assertEqual(household.claim(s, 'k', 'ana', 0, 30), (True, None))
        self.assertEqual(household.claim(s, 'k', 'bo', 60, 30), (False, 'ana'))
        self.assertEqual(household.claim(s, 'k', 'bo', 31 * 60, 30), (True, None))   # ana's heartbeat stopped: expired
        s['members'] = {'bo': {'name': 'Bo'}}
        household.leave(s, 'bo')
        self.assertEqual(s['leases'], {})


class ReviewFixesTest(unittest.TestCase):
    def test_only_the_publisher_or_the_lease_holder_closes_a_task(self):
        s = household.new_state('home', 0)
        s['members'] = {'ana': {'name': 'Ana', 'joined': 0}, 'bo': {'name': 'Bo', 'joined': 1}, 'cy': {'name': 'Cy', 'joined': 2}}
        task = {'title': 'T', 'check': 'true'}
        household.sync_project(s, 'p/x', {'publish': [task]}, 'ana', 0, 30)
        household.sync_project(s, 'p/x', {'closed': [{'key': 't', 'status': 'done'}]}, 'cy', 1, 30)
        self.assertEqual(s['projects']['p/x']['tasks']['t']['status'], 'queued')   # Cy neither published nor holds it
        household.sync_project(s, 'p/x', {'held': ['t']}, 'bo', 2, 30)
        household.sync_project(s, 'p/x', {'closed': [{'key': 't', 'status': 'done'}]}, 'bo', 3, 30)
        self.assertEqual(s['projects']['p/x']['tasks']['t']['status'], 'done')

    def test_the_admin_who_leaves_hands_over_to_the_longest_member(self):
        s = household.new_state('home', 0)
        s['members'] = {'ana': {'name': 'Ana', 'joined': 0}, 'bo': {'name': 'Bo', 'joined': 5}, 'cy': {'name': 'Cy', 'joined': 9}}
        s['admin'] = 'ana'
        household.leave(s, 'ana')
        self.assertEqual(s['admin'], 'bo')

    def test_a_replayed_request_is_refused_after_a_restart_too(self):
        h = Household(self)
        self.addCleanup(h.close)
        m = h.membership(h.ana_home)
        raw = b'{}'
        ts, nonce = str(int(h.clock())), 'n' * 24
        headers = {'X-Tutti-User': m['user_id'], 'X-Tutti-Time': ts, 'X-Tutti-Nonce': nonce,
                   'X-Tutti-Sign': household.signature(m['token'], 'POST', '/hh/status', ts, nonce, raw)}
        self.assertEqual(h.service.handle('/hh/status', headers, raw, '127.0.0.1')[0], 200)
        fresh = coordinator.Service(h.ana, h.clock)   # the coordinator restarted: the nonces are in its state file
        self.assertEqual(fresh.handle('/hh/status', headers, raw, '127.0.0.1')[0], 401)

    def test_init_starts_a_fresh_household(self):
        with tempfile.TemporaryDirectory() as home, as_user(home):
            cfg = make_cfg(home, 'Ana')
            coordinator.save(coordinator.state_path(cfg), {**household.new_state('old', 0), 'members': {'x': {'name': 'Old'}}})
            coordinator.init(cfg, 'new', host='127.0.0.1', port=4778)
            data = coordinator.load(coordinator.state_path(cfg))
        self.assertEqual((data['household'], [m['name'] for m in data['members'].values()]), ('new', ['Ana']))

    def test_a_shared_repo_without_origin_holds_its_tasks_and_cannot_be_shared(self):
        with tempfile.TemporaryDirectory() as home, as_user(home):
            cfg = make_cfg(home, 'Ana', shared=False)
            subprocess.run(['git', '-C', cfg['autopilot']['repos']['app']['path'], 'remote', 'remove', 'origin'], check=True)
            sharing._keys.clear()
            out = io.StringIO()
            self.assertEqual(householdcmd.cmd_household(cfg, SimpleNamespace(action='project', arg=['share', 'app']), out), 2)
            data = {'tasks': [{'id': 't1', 'repo': 'app', 'title': 'x', 'status': 'queued'}]}
            self.assertIn('no origin remote', sharing.no_key_hold(data, {'app': ''})['t1'])


class SharedBacklogTest(unittest.TestCase):
    def test_two_members_publish_one_task_once_and_only_one_holds_it(self):
        s = household.new_state('home', 0)
        s['members'] = {'ana': {'name': 'Ana'}, 'bo': {'name': 'Bo'}}
        task = {'title': 'Fix the login page', 'scope': ['web/login.js'], 'check': 'true', 'size': 'S', 'source': 'todo'}
        a = household.sync_project(s, 'github.com/octo/app', {'publish': [task], 'want': ['fix the login page']}, 'ana', 0, 30)
        b = household.sync_project(s, 'github.com/octo/app', {'publish': [{**task, 'title': 'fix the LOGIN page.'}],
                                                               'want': ['fix the login page']}, 'bo', 1, 30)
        self.assertEqual(len(a['tasks']), 1)
        self.assertEqual(len(b['tasks']), 1)
        self.assertEqual((a['mine'], b['mine'], b['held']), (['fix the login page'], [], {'fix the login page': 'Ana'}))
        household.sync_project(s, 'github.com/octo/app', {'closed': [{'key': 'fix the login page', 'status': 'done'}]}, 'ana', 2, 30)
        b = household.sync_project(s, 'github.com/octo/app', {'publish': [task]}, 'bo', 3, 30)
        self.assertEqual((b['tasks'][0]['status'], b['tasks'][0]['closed_by'], b['held']), ('done', 'Ana', {}))


class Household:
    """Ana runs the coordinator; Bo joins it over real HTTP on 127.0.0.1."""

    def __init__(self, test):
        self.test = test
        self.clock = Clock()
        self.ana_home, self.bo_home = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.ana = make_cfg(self.ana_home, 'Ana')
        with as_user(self.ana_home):
            self.code = coordinator.init(self.ana, 'home', host='127.0.0.1', port=4778, clock=self.clock)
        self.service = coordinator.Service(self.ana, self.clock)
        self.httpd = coordinator.server(self.ana, port=0, bind='127.0.0.1', service=self.service)
        import threading
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.code = self.code.replace(':4778:', f':{self.port}:')
        self.bo = make_cfg(self.bo_home, 'Bo')

    def service_state(self):
        return coordinator.load(coordinator.state_path(self.ana))

    def membership(self, home):
        with as_user(home):
            return users.read_household()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        import shutil
        for home in (self.ana_home, self.bo_home):   # never left in /tmp (docs/LESSONS.md, the test suite filled /tmp)
            shutil.rmtree(home, ignore_errors=True)


class CoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.h = Household(self)
        self.addCleanup(self.h.close)

    def test_bo_joins_with_the_code_and_both_see_the_household(self):
        with as_user(self.h.bo_home):
            answer = coordinator.join(self.h.bo, self.h.code, 'Bo', 'cursor', clock=self.h.clock)
            self.assertEqual(answer['household'], 'home')
            self.assertEqual(users.identity(self.h.bo)['role'], 'member')
            bo = coordinator.Client(users.read_household(), clock=self.h.clock)
            v = bo.call('/hh/status')
        self.assertEqual(sorted(m['name'] for m in v['members']), ['Ana', 'Bo'])
        self.assertEqual(v['admin'], 'Ana')
        token = self.h.membership(self.h.bo_home)['token']
        for body in (answer, v):   # the token is in no answer: both sides derive it
            self.assertNotIn(token, str(body))
            self.assertNotIn('token', str(body))

    def test_a_wrong_code_is_refused_and_ten_close_the_join(self):
        bad = self.h.code[:-4] + 'AAAA'
        with as_user(self.h.bo_home):
            with self.assertRaisesRegex(household.HouseholdError, 'wrong or old'):
                coordinator.join(self.h.bo, bad, 'Bo', 'cursor', clock=self.h.clock)
            for _ in range(9):
                with self.assertRaises(household.HouseholdError):
                    coordinator.join(self.h.bo, bad, 'Bo', 'cursor', clock=self.h.clock)
            with self.assertRaisesRegex(household.HouseholdError, 'too many'):
                coordinator.join(self.h.bo, self.h.code, 'Bo', 'cursor', clock=self.h.clock)

    def test_an_expired_code_and_a_forged_member_are_refused(self):
        with as_user(self.h.bo_home):
            self.h.clock.t += 8 * 86400
            with self.assertRaisesRegex(household.HouseholdError, 'expired'):
                coordinator.join(self.h.bo, self.h.code, 'Bo', 'cursor', clock=self.h.clock)
        forged = coordinator.Client({'coordinator': f'127.0.0.1:{self.h.port}', 'user_id': 'f' * 16, 'token': 'x'}, clock=self.h.clock)
        with self.assertRaisesRegex(household.HouseholdError, 'not a member'):
            forged.call('/hh/status')

    def test_the_coordinator_state_is_mode_600(self):
        self.assertEqual(os.stat(coordinator.state_path(self.h.ana)).st_mode & 0o777, 0o600)

    def test_unreachable_is_its_own_error(self):
        c = coordinator.Client({'coordinator': '127.0.0.1:1', 'user_id': 'u', 'token': 't'}, timeout=0.5)
        with self.assertRaises(coordinator.Unreachable):
            c.call('/hh/status')

    def test_leave_ends_membership_and_status_says_household_of_one(self):
        with as_user(self.h.bo_home):
            coordinator.join(self.h.bo, self.h.code, 'Bo', 'cursor', clock=self.h.clock)
            out = io.StringIO()
            sharing.set_shared(self.h.bo, 'app', True)
            rc = householdcmd.cmd_household(self.h.bo, SimpleNamespace(action='leave', arg=[]), out,
                                            client_fn=lambda cfg: coordinator.Client(users.read_household(), clock=self.h.clock))
            self.assertEqual(rc, 0)
            self.assertEqual(users.read(config.state_dir(self.h.bo)).get('shared_repos'), None)
            self.assertIn('Left the household', out.getvalue())
            self.assertEqual(users.identity(self.h.bo)['role'], 'alone')


class TwoUsersOneProjectTest(unittest.TestCase):
    """Ana and Bo share the project app; each has a local backlog and runs autopilot rounds against the coordinator."""

    def setUp(self):
        self.h = Household(self)
        self.addCleanup(self.h.close)
        with as_user(self.h.bo_home):
            coordinator.join(self.h.bo, self.h.code, 'Bo', 'cursor', clock=self.h.clock)
        self.clients = {n: DirectClient(self.h.service, self.h.membership(home), self.h.clock)
                        for n, home in (('ana', self.h.ana_home), ('bo', self.h.bo_home))}
        self.cfgs = {'ana': self.h.ana, 'bo': self.h.bo}
        sharing._keys.clear()

    def add(self, who, title):
        with backlog.locked(self.cfgs[who]) as data:
            return backlog.add(data, backlog.new_task('app', title, (), 'true'))[0]['id']

    def round(self, who):
        """sync outside the lock, then under it: the plan's hold and the dispatch decision (no worker is started)."""
        cfg = self.cfgs[who]
        result = sharing.sync(cfg, backlog.load(cfg), {'prompting': False}, self.clients[who], self.h.clock())
        with backlog.locked(cfg) as data:
            hold, events = sharing.under_lock(result, data, self.h.clock())
            states = {'agy-claude': {'name': 'agy-claude', 'verdict': 'behind', 'room_tasks': 9, 'used': 10.0, 'panic': False,
                                     'panic_at': 95}}
            p = autopilot.plan(cfg, data['tasks'], states, [], True, None, hold)
            for task_id, _step in p['dispatch']:
                backlog.find(data, task_id)['status'] = 'running'
        return p, events

    def test_the_same_task_found_by_both_runs_once(self):
        self.add('ana', 'Add unit tests for tutti/x.py')
        self.add('bo', 'add unit tests for tutti/x.py')
        pa, _ = self.round('ana')
        pb, _ = self.round('bo')
        self.assertEqual(len(pa['dispatch']), 1)
        self.assertEqual(pb['dispatch'], [])
        self.assertIn('leased by Ana', ' '.join(pb['waiting'].values()))

    def test_a_task_one_adds_is_done_by_the_other_and_closed_for_both(self):
        self.add('bo', 'Fix the flaky login test')
        self.round('bo')                         # published; Bo reserves it, but Bo's account is busy: say it stays queued
        with backlog.locked(self.cfgs['bo']) as data:
            data['tasks'][0]['status'] = 'queued'
        self.h.clock.t += 6 * 60                 # Bo's reservation (5 min) lapses without a start
        pa, events = self.round('ana')           # mirrored into Ana's backlog; reserved for her at her next round
        self.assertIn('from the household', ' '.join(events))
        self.assertEqual(pa['dispatch'], [])
        pa, _ = self.round('ana')
        self.assertEqual(len(pa['dispatch']), 1)  # Ana's own account takes Bo's shared task
        with backlog.locked(self.cfgs['ana']) as data:
            data['tasks'][0]['status'] = 'done'
        self.round('ana')
        _pb, events = self.round('bo')
        self.assertEqual(backlog.load(self.cfgs['bo'])['tasks'][0]['status'], 'done')
        self.assertIn('done by Ana', ' '.join(events))

    def test_an_unreachable_coordinator_holds_shared_tasks_but_not_private_ones(self):
        self.cfgs['ana']['autopilot']['repos']['own'] = {'path': self.h.ana_home, 'check': 'true'}
        self.add('ana', 'shared work')
        with backlog.locked(self.cfgs['ana']) as data:
            backlog.add(data, backlog.new_task('own', 'private work', (), 'true'))
        self.clients['ana'].down = True
        p, _ = self.round('ana')
        sent = [backlog.find(backlog.load(self.cfgs['ana']), i)['title'] for i, _ in p['dispatch']]
        self.assertEqual(sent, ['private work'])
        self.assertIn('not reachable', ' '.join(p['waiting'].values()))

    def test_a_household_of_one_makes_no_call(self):
        with tempfile.TemporaryDirectory() as home, as_user(home):
            cfg = make_cfg(home, 'Solo')
            result = sharing.sync(cfg, backlog.load(cfg))
        self.assertEqual((result['hold'], result['answer'], result['error']), ({}, None, None))


class AwayFromHomeTest(TwoUsersOneProjectTest):
    """Bo takes his laptop away with no VPN: his Tutti keeps working on its own and syncs when he is home again."""

    def test_offline_then_sync(self):
        self.add('bo', 'shared before leaving')
        self.round('bo')                                         # published and started (running) while at home
        self.assertEqual(backlog.load(self.cfgs['bo'])['tasks'][0]['status'], 'running')
        self.cfgs['bo']['autopilot']['repos']['own'] = {'path': self.h.bo_home, 'check': 'true'}
        self.clients['bo'].down = True                           # away: the coordinator is on the home network only
        with as_user(self.h.bo_home):
            self.add('bo', 'shared while away')
            with backlog.locked(self.cfgs['bo']) as data:
                backlog.add(data, backlog.new_task('own', 'private while away', (), 'true'))
                data['tasks'][0]['status'] = 'done'              # the running task finished on his own
            p, _ = self.round('bo')
            sent = sorted(backlog.find(backlog.load(self.cfgs['bo']), i)['title'] for i, _ in p['dispatch'])
            self.assertEqual(sent, ['private while away'])       # own work goes on; no shared task starts, no lease is taken
            self.h.clock.t += 31 * 60
            v = sharing.view(self.cfgs['bo'], self.h.clock())
        self.assertTrue(v['away'])
        self.assertEqual(v['away_note'], "away from the household: working on your own until you're home")
        live = [k for k, lease in self.h.service_state()['leases'].items()   # his leases ran out; he renewed none while away
                if lease['user'] == self.bo_id() and lease['until'] > self.h.clock()]
        self.assertEqual(live, [])
        self.clients['bo'].down = False                          # home again
        with as_user(self.h.bo_home):
            _p, events = self.round('bo')
            v = sharing.view(self.cfgs['bo'], self.h.clock())
        self.assertIn('back home: synced', ' '.join(events))
        self.assertFalse(v['away'])
        tasks = self.h.service_state()['projects']['github.com/octo/app']['tasks']
        self.assertEqual(tasks['shared before leaving']['status'], 'done')   # what happened while away reached the household
        self.assertIn('shared while away', tasks)

    def bo_id(self):
        return self.h.membership(self.h.bo_home)['user_id']


class CommandsTest(unittest.TestCase):
    def test_project_share_and_unshare_without_editing_the_config(self):
        with tempfile.TemporaryDirectory() as home, as_user(home):
            cfg = make_cfg(home, 'Ana', shared=False)
            out = io.StringIO()
            self.assertEqual(householdcmd.cmd_household(cfg, SimpleNamespace(action='project', arg=['share', 'app']), out), 0)
            self.assertEqual(sharing.shared_repos(cfg), ['app'])
            householdcmd.cmd_household(cfg, SimpleNamespace(action='project', arg=['unshare', 'app']), out)
            self.assertEqual(sharing.shared_repos(cfg), [])
            self.assertEqual(householdcmd.cmd_household(cfg, SimpleNamespace(action='project', arg=['share', 'nope']), out), 2)

    def test_join_without_a_code_and_status_while_offline_say_what_to_do(self):
        with tempfile.TemporaryDirectory() as home, as_user(home):
            cfg = make_cfg(home, 'Bo')
            self.assertEqual(householdcmd.cmd_household(cfg, SimpleNamespace(action='join', arg=[], name=None), io.StringIO()), 2)
            users.write_household({'household': 'home', 'coordinator': '127.0.0.1:1', 'user_id': 'u' * 16, 'token': 't',
                                   'role': 'member'})
            out = io.StringIO()
            rc = householdcmd.cmd_household(cfg, SimpleNamespace(action='status', arg=[], json=False), out,
                                            client_fn=lambda c: coordinator.Client(users.read_household(), timeout=0.5))
            self.assertEqual(rc, 1)
            self.assertIn('Working alone', out.getvalue())

    def test_init_asks_only_the_name_and_prints_the_code(self):
        with tempfile.TemporaryDirectory() as home, as_user(home):
            cfg = make_cfg(home, 'Ana')
            out = io.StringIO()
            rc = householdcmd.cmd_household(cfg, SimpleNamespace(action='init', arg=[], name=None, host='192.168.1.10', port=4778), out,
                                            io={'isatty': lambda: True, 'input': lambda _q: 'Casa'})
            self.assertEqual(rc, 0)
            self.assertIn('tutti household join tutti-hh:192.168.1.10:4778:', out.getvalue())
            self.assertEqual(users.identity(cfg)['role'], 'coordinator')
            again = householdcmd.cmd_household(cfg, SimpleNamespace(action='init', arg=[], name='x', host=None, port=None), out)
            self.assertEqual(again, 2)


HAS_AUTH = __import__('importlib').util.find_spec('tutti.invites') is not None


@unittest.skipUnless(HAS_AUTH, 'the dashboard sign-in (tutti/auth.py, tutti/invites.py) lands with #39')
class InviteJoinTest(unittest.TestCase):
    """The dashboard's admin (Ana, signed in with Google) invites Bo; Bo runs one command: `tutti household join <link>`."""

    def setUp(self):
        from tutti import auth, invites
        self.clock = Clock()
        self.ana_home, self.bo_home = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.ana, self.bo = make_cfg(self.ana_home, 'Ana'), make_cfg(self.bo_home, 'Bo')
        self.folder = config.state_dir(self.ana)
        self.users = auth.Users(self.folder, clock=self.clock)
        self.users.sign_in({'sub': 'g-ana', 'email': 'ana@example.com', 'name': 'Ana B'})
        self.invites = invites.Invites(self.folder, clock=self.clock)
        self.token, _ = self.invites.create('viewer', by='g-ana')
        self.link = invites.url('http://127.0.0.1:4777', self.token)
        self.service = coordinator.Service(self.ana, self.clock)
        self.httpd = coordinator.server(self.ana, port=0, bind='127.0.0.1', service=self.service)
        import threading
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.httpd.shutdown(), self.httpd.server_close()))
        self.where = f'127.0.0.1:{self.httpd.server_address[1]}'
        with as_user(self.ana_home):   # what the first invite join does on the admin's dashboard (one process per login)
            self.founded = coordinator.founding(self.ana, self.clock())

    def join(self, sign_in=True):
        opened, slept = [], []

        def sleep(_s):   # while the CLI waits, Bo signs in with Google on the invite page
            slept.append(1)
            if sign_in:
                self.users.sign_in({'sub': 'g-bo', 'email': 'bo@example.com', 'name': 'Bo C'}, invite=self.token, invites=self.invites)
            else:
                self.clock.t += 61
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': os.path.join(self.bo_home, 'xdg')}):
            answer = coordinator.join_invite(self.bo, self.link, 'cursor', ['cursor'], override=self.where, clock=self.clock,
                                             sleep=sleep, open_fn=opened.append)
            me = users.identity(self.bo)
        return answer, me, opened, slept

    def test_one_command_pairs_bo_with_his_google_identity(self):
        self.assertEqual((self.founded['household'], self.founded['admin']), ("Ana's household", 'g-ana'))
        with as_user(self.ana_home):
            self.assertEqual((users.read_household()['role'], users.identity(self.ana)['id']), ('coordinator', 'g-ana'))
        answer, me, opened, slept = self.join()
        self.assertEqual((answer['sub'], answer['email'], answer['name']), ('g-bo', 'bo@example.com', 'Bo C'))
        self.assertEqual((opened, len(slept)), ([self.link], 1))   # the link opened once, while nobody had signed in
        self.assertEqual((me['id'], me['email'], me['role']), ('g-bo', 'bo@example.com', 'member'))
        joined = household.members_by_sub(self.folder)
        self.assertEqual(sorted(joined), ['g-ana', 'g-bo'])   # the Users card: "Tutti joined, last seen"
        self.assertEqual(joined['g-bo']['agent'], 'cursor')
        self.assertIn('household', self.invites.listing()[0]['used'])

    def test_the_same_invite_cannot_join_twice_and_a_wrong_link_is_refused(self):
        self.join()
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': os.path.join(tempfile.mkdtemp(), 'xdg')}):
            with self.assertRaisesRegex(household.HouseholdError, 'already used'):
                coordinator.join_invite(self.bo, self.link, 'cursor', [], override=self.where, clock=self.clock, sleep=lambda s: None)
            with self.assertRaisesRegex(household.HouseholdError, 'not a Tutti invite link'):
                coordinator.join_invite(self.bo, 'https://x.example/nope', 'cursor', [], override=self.where)

    def test_nobody_signs_in_within_two_minutes(self):
        with self.assertRaisesRegex(household.HouseholdError, 'sign in with Google'):
            self.join(sign_in=False)

class GuidedJoinTest(unittest.TestCase):
    """`tutti household join <invite-url>` follows the guided rule: detect, default, the human steps once at the end."""

    LINK = 'http://192.168.1.10:4777/invite#abc'

    def test_the_cli_detects_the_agent_and_asks_nothing(self):
        out = io.StringIO()
        bo_home = tempfile.mkdtemp()
        bo = make_cfg(bo_home, 'Bo')
        fake = mock.Mock(return_value={'household': 'Ana', 'name': 'Bo C', 'sub': 'g-bo', 'email': 'bo@example.com'})
        with as_user(bo_home), mock.patch.object(householdcmd, 'detect_main',
                                                  return_value=('cursor', {'key': 'main.agent', 'value': 'cursor', 'why': 'w'})):
            rc = householdcmd.join(bo, SimpleNamespace(arg=[self.LINK], name=None, host=None), out, {}, None, invite_fn=fake)
        self.assertEqual(rc, 0)
        self.assertIn('Joined "Ana" as Bo C (bo@example.com)', out.getvalue())
        self.assertIn('Sign in to cursor once', out.getvalue())   # no cursor account in the config: one step, at the end
        self.assertEqual(fake.call_args.args[2], 'cursor')

    def test_not_signed_in_yet_is_one_step_at_the_end(self):
        out = io.StringIO()
        bo_home = tempfile.mkdtemp()
        fake = mock.Mock(side_effect=household.HouseholdError('sign in with Google on the invite page first'))
        with as_user(bo_home):
            rc = householdcmd.join(make_cfg(bo_home, 'Bo'), SimpleNamespace(arg=[self.LINK], name=None, host=None), out, {}, None,
                                   invite_fn=fake)
        self.assertEqual(rc, 1)
        self.assertIn('1. Sign in with Google on the invite page', out.getvalue())

    def test_detect_main_never_asks(self):
        cfg = make_cfg(tempfile.mkdtemp(), 'Bo')
        cfg['main']['agent'] = 'cursor'
        self.assertEqual(householdcmd.detect_main(cfg, which=lambda n: n == 'cursor-agent')[0], 'cursor')
        cfg['main']['agent'] = 'claude'
        kind, choice = householdcmd.detect_main(cfg, which=lambda n: n in ('gemini', 'codex'))
        self.assertEqual((kind, choice['key']), ('codex', 'main.agent'))
        self.assertIn('also: gemini', choice['why'])
        self.assertEqual(householdcmd.detect_main(cfg, which=lambda n: False)[0], 'claude')

    def test_the_invite_page_shows_one_install_line_that_joins(self):
        lines = householdcmd.invite_lines(self.LINK)
        self.assertIn(f'--join "{self.LINK}"', lines['linux'])
        self.assertIn(f'-Join "{self.LINK}"', lines['win32'])


HOUSEHOLD_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tutti', 'web', 'household.js')


def js(expr):
    import json
    with open(HOUSEHOLD_JS, encoding='utf8') as f:
        src = f.read()
    out = subprocess.run(['node', '-e', f'{src}\nconsole.log(JSON.stringify({expr}));'], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(__import__('shutil').which('node'), 'node is not installed')
class DashboardHouseholdTest(unittest.TestCase):
    STATE = {'role': 'member', 'household': 'Ana\'s household', 'me': 'Bo', 'reachable': True, 'at': 1000,
             'members': [{'name': 'Ana', 'agent': 'claude', 'active': True, 'online': True, 'runs_today': 4},
                         {'name': 'Bo', 'agent': 'cursor', 'active': False, 'online': True, 'runs_today': 1, 'me': True}],
             'machines': {'pc': {'owner': 'Ana', 'slots': 4, 'use': {'Ana': 3, 'Bo': 1}, 'grant': 1, 'fairness': 0.9}}}

    def test_members_show_their_main_agent_and_who_is_prompting(self):
        r = js(f'householdRows({__import__("json").dumps(self.STATE)}, 1010000)')
        self.assertEqual([(m['name'], m['agent'], m['state']) for m in r['members']],
                         [('Ana', 'Claude Code', 'prompting now'), ('Bo (you)', 'Cursor', 'online')])
        self.assertIn('Ana 3, you 1', r['machines'][0]['note'])
        self.assertIn('fairness 90 %', r['machines'][0]['note'])

    def test_offline_and_alone_say_what_to_do(self):
        r = js(f'householdRows({__import__("json").dumps({**self.STATE, "reachable": False, "error": "timeout"})})')
        self.assertIn('working alone', r['note'])
        self.assertIn('Invite someone', js('householdRows({role: "alone"})')['note'])

    def test_others_jobs_show_only_as_busy_for(self):
        m = {'household_use': {'owner': 'Ana', 'grant': 1, 'others': {'Ana': 3}}}
        self.assertEqual(js(f'busyFor({__import__("json").dumps(m)})'), 'household: busy for Ana (3) · your share 1')
        self.assertEqual(js('busyFor({})'), '')


if __name__ == '__main__':
    unittest.main()
