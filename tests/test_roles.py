import unittest

from tutti.roles import check_roles

class TestRoles(unittest.TestCase):
    def test_worker_role(self):
        # Machine with GPU
        info = {'gpu': 'RTX 3060'}
        roles = check_roles(info, kind='machine')
        self.assertTrue(roles['worker'][0])

        # Machine without GPU/NPU
        info = {'gpu': '', 'npu': ''}
        roles = check_roles(info, kind='machine')
        self.assertFalse(roles['worker'][0])
        self.assertEqual(roles['worker'][1], 'no GPU or NPU detected')

        # Watch
        info = {'gpu': 'RTX 3060'}
        roles = check_roles(info, kind='watch')
        self.assertFalse(roles['worker'][0])
        self.assertEqual(roles['worker'][1], 'watch too weak')

    def test_device_host_role(self):
        # Machine with adb
        info = {'loaded': ['adb']}
        roles = check_roles(info, kind='machine')
        self.assertTrue(roles['device_host'][0])

        # Machine without adb
        info = {'loaded': []}
        roles = check_roles(info, kind='machine')
        self.assertFalse(roles['device_host'][0])

    def test_cast_receiver_role(self):
        # Device that is a TV
        info = {'kind': 'tv'}
        roles = check_roles(info, kind='device')
        self.assertTrue(roles['cast_receiver'][0])

        # Device that is not a TV
        info = {'kind': 'phone'}
        roles = check_roles(info, kind='device')
        self.assertFalse(roles['cast_receiver'][0])

    def test_additional_roles(self):
        info = {'loaded': ['adb', 'sensors', 'energy_meter', 'browser', 'git']}
        roles = check_roles(info, kind='machine')
        self.assertTrue(roles['presence_source'][0])
        self.assertTrue(roles['energy_meter'][0])
        self.assertTrue(roles['notification_target'][0])
        self.assertTrue(roles['dashboard_viewer'][0])
        self.assertTrue(roles['ci_runner'][0])

        info = {'loaded': []}
        roles = check_roles(info, kind='machine')
        self.assertFalse(roles['presence_source'][0])
        self.assertFalse(roles['energy_meter'][0])
        self.assertFalse(roles['dashboard_viewer'][0])
        self.assertFalse(roles['ci_runner'][0])

if __name__ == '__main__':
    unittest.main()
