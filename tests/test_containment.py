"""Conservative containment-candidate classification from fake data only; never touches Docker or a machine."""
import copy
import unittest

from tutti import containment


class CandidateTest(unittest.TestCase):
    def setUp(self):
        self.expected = {'run': 'a' * 32, 'container': 'b' * 64, 'boot': '123e4567-e89b-12d3-a456-426614174000',
                         'engine': 'engine.example', 'workspace': '/workspace/project'}
        self.inspect = {'Id': self.expected['container'],
                        'Config': {'Labels': {containment.LABEL: self.expected['run']}, 'User': '1000:1000'},
                        'HostConfig': {'NetworkMode': 'none', 'Privileged': False, 'AutoRemove': False,
                                       'PidMode': 'private', 'IpcMode': 'private', 'CgroupnsMode': 'private',
                                       'UsernsMode': 'private', 'ReadonlyRootfs': True, 'CapAdd': None,
                                       'CapDrop': ['ALL'], 'Devices': [], 'DeviceRequests': [], 'VolumesFrom': [],
                                       'SecurityOpt': ['no-new-privileges:true'], 'RestartPolicy': {'Name': 'no'}},
                        'Mounts': [{'Type': 'bind', 'Source': '/workspace/project',
                                    'Destination': '/workspace/project', 'RW': True}],
                        'State': {'Status': 'exited', 'Running': False, 'Restarting': False, 'Paused': False, 'Dead': False,
                                  'Pid': 0, 'ExitCode': 0, 'FinishedAt': '2026-11-18T12:00:00Z'}}
        self.snapshot = {'engine': self.expected['engine'], 'boot': self.expected['boot'], 'inspect': self.inspect}

    def test_narrow_stopped_candidate_is_not_a_stop_authorization(self):
        self.assertEqual(containment.classify(self.expected, self.snapshot), 'stopped-candidate')
        self.inspect['State'].update(Status='running', Running=True, Pid=42)
        self.assertEqual(containment.classify(self.expected, self.snapshot), 'observed-running')

    def test_missing_wrong_or_ambiguous_identity_stays_unknown(self):
        for key, value in (('run', ''), ('container', 'b' * 12), ('boot', 'bad'), ('engine', '')):
            with self.subTest(key=key):
                self.assertEqual(containment.classify({**self.expected, key: value}, self.snapshot), 'unknown')
        for key, value in (('boot', 'different'), ('engine', 'different'), ('inspect', None)):
            with self.subTest(key=key):
                self.assertEqual(containment.classify(self.expected, {**self.snapshot, key: value}), 'unknown')
        self.inspect['Config']['Labels'][containment.LABEL] = 'c' * 32
        self.assertEqual(containment.classify(self.expected, self.snapshot), 'unknown')

    def test_unsafe_host_settings_never_become_a_stopped_candidate(self):
        cases = {'NetworkMode': 'bridge', 'Privileged': True, 'AutoRemove': True, 'PidMode': 'host',
                 'IpcMode': 'host', 'CgroupnsMode': 'host', 'UsernsMode': 'host', 'ReadonlyRootfs': False,
                 'CapAdd': ['SYS_ADMIN'], 'CapDrop': [], 'Devices': ['/dev/x'], 'DeviceRequests': [{'Count': -1}],
                 'VolumesFrom': ['other'], 'Tmpfs': {'/tmp': 'rw'}, 'SecurityOpt': ['seccomp=unconfined'],
                 'RestartPolicy': {'Name': 'always'}}
        for key, value in cases.items():
            with self.subTest(key=key):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot['inspect']['HostConfig'][key] = value
                self.assertEqual(containment.classify(self.expected, snapshot), 'unknown')

    def test_missing_isolation_settings_and_conflicting_security_options_are_unknown(self):
        for key in ('CapAdd', 'Devices', 'DeviceRequests', 'VolumesFrom'):
            with self.subTest(key=key):
                snapshot = copy.deepcopy(self.snapshot)
                del snapshot['inspect']['HostConfig'][key]
                self.assertEqual(containment.classify(self.expected, snapshot), 'unknown')
        for option in ('no-new-privileges=false', 'no-new-privileges:false', 'No-New-Privileges:true'):
            with self.subTest(option=option):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot['inspect']['HostConfig']['SecurityOpt'].append(option)
                self.assertEqual(containment.classify(self.expected, snapshot), 'unknown')
        self.inspect['HostConfig']['SecurityOpt'].append('seccomp=unconfined')
        self.assertEqual(containment.classify(self.expected, self.snapshot), 'unknown')
        self.inspect['HostConfig']['SecurityOpt'].pop()
        self.inspect['HostConfig']['Tmpfs'] = {}
        self.assertEqual(containment.classify(self.expected, self.snapshot), 'stopped-candidate')

    def test_docker_finish_time_requires_complete_timestamp_and_accepts_nanoseconds(self):
        for stamp in ('2026-11-18T12:00:00Z', '2026-11-18T12:00:00.123456789Z', '2026-11-18T12:00:00+00:00'):
            with self.subTest(stamp=stamp):
                self.inspect['State']['FinishedAt'] = stamp
                self.assertEqual(containment.classify(self.expected, self.snapshot), 'stopped-candidate')
        for stamp in ('2026-11-18X12:00:00Z', '2026-11-18T12:00:00', '2026-11-18T12:00:00.1234567890Z'):
            with self.subTest(stamp=stamp):
                self.inspect['State']['FinishedAt'] = stamp
                self.assertEqual(containment.classify(self.expected, self.snapshot), 'unknown')

    def test_running_requires_consistent_state_flags(self):
        self.inspect['State'].update(Status='running', Running=True, Pid=42)
        self.assertEqual(containment.classify(self.expected, self.snapshot), 'observed-running')
        for key in ('Restarting', 'Paused', 'Dead'):
            with self.subTest(key=key):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot['inspect']['State'][key] = True
                self.assertEqual(containment.classify(self.expected, snapshot), 'unknown')
        for pid in (0, None, False):
            with self.subTest(pid=pid):
                self.inspect['State']['Pid'] = pid
                self.assertEqual(containment.classify(self.expected, self.snapshot), 'unknown')

    def test_mounts_and_user_must_be_strictly_bounded(self):
        for user in ('0:1000', '1000:0', 'root', '', None):
            with self.subTest(user=user):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot['inspect']['Config']['User'] = user
                self.assertEqual(containment.classify(self.expected, snapshot), 'unknown')
        for path in ('/', '/workspace/../etc', '/workspace//project', '/workspace/project/', '/workspace/./project',
                     '/workspace/' + chr(0) + 'bad'):
            with self.subTest(path=path):
                self.assertEqual(containment.classify({**self.expected, 'workspace': path}, self.snapshot), 'unknown')
        for extra in ({'Type': 'bind', 'Source': '/var/run/docker.sock', 'Destination': '/var/run/docker.sock', 'RW': True},
                      {'Type': 'volume', 'Source': '/other', 'Destination': '/other', 'RW': True}):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot['inspect']['Mounts'].append(extra)
            self.assertEqual(containment.classify(self.expected, snapshot), 'unknown')

    def test_only_unambiguously_exited_state_is_a_candidate(self):
        cases = {'Status': 'dead', 'Running': None, 'Restarting': True, 'Paused': True, 'Dead': True, 'Pid': False,
                 'ExitCode': True, 'FinishedAt': '0001-01-01T00:00:00Z'}
        for key, value in cases.items():
            with self.subTest(key=key):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot['inspect']['State'][key] = value
                self.assertEqual(containment.classify(self.expected, snapshot), 'unknown')
        del self.inspect['State']['Paused']
        self.assertEqual(containment.classify(self.expected, self.snapshot), 'unknown')


if __name__ == '__main__':
    unittest.main()
