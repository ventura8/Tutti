"""Room on the temp folder and the work disk: under 10 % free file slots, or 10 % free space with little left, Tutti warns
in plain words and starts no new work (tutti/diskroom.py)."""
import os
import unittest
from types import SimpleNamespace

import tmpdirs

from tutti import autopilot, config, diskroom


def fs(files=1000, ffree=500, blocks=1000, bfree=500, frsize=4096):
    return lambda path: SimpleNamespace(f_files=files, f_favail=ffree, f_blocks=blocks, f_bavail=bfree, f_frsize=frsize)


class DiskRoomTest(unittest.TestCase):
    def setUp(self):
        d = tmpdirs.mkdtemp()
        self.cfg = config.normalise({'project': {'worktrees': d, 'state': os.path.join(d, 'state')}})

    def test_plenty_of_room_says_nothing(self):
        self.assertEqual(diskroom.problems(self.cfg, fs()), [])

    def test_few_file_slots_name_the_folder_why_and_the_fix(self):
        out = diskroom.problems(self.cfg, fs(ffree=40))
        self.assertEqual(len(out), 1)   # one disk: said once
        for words in ('4 % of its file slots', 'starts no new work', 'Fix: delete old folders'):
            self.assertIn(words, out[0])

    def test_low_space_counts_only_when_little_is_left(self):
        self.assertIn('free space', diskroom.problems(self.cfg, fs(bfree=50))[0])   # 5 % of a small disk
        big = fs(blocks=10_000_000, bfree=900_000)                                  # 9 % of a 40 GB disk: 3.7 GB
        self.assertTrue(diskroom.problems(self.cfg, big))
        huge = fs(blocks=250_000_000, bfree=20_000_000)                             # 8 % of 1 TB: 82 GB left
        self.assertEqual(diskroom.problems(self.cfg, huge), [])

    def test_an_unreadable_disk_is_no_alarm(self):
        def broken(path):
            raise OSError('gone')
        self.assertEqual(diskroom.problems(self.cfg, broken), [])

    def test_the_autopilot_starts_nothing_while_a_disk_is_low(self):
        self.cfg['_disk'] = ['the temp folder (/tmp) has only 2 % of its file slots left']
        free, stop = autopilot.capacity_now(self.cfg, [], [], None)
        self.assertEqual(free, (0, 0))
        self.assertIn('file slots', stop['reason'])



class WindowsHasNoStatvfs(unittest.TestCase):
    def test_free_space_is_read_from_disk_usage_where_statvfs_is_missing(self):
        from collections import namedtuple
        from unittest import mock
        usage = namedtuple('usage', 'total used free')(1000, 900, 100)
        real = diskroom.os
        fake = mock.Mock(spec=[n for n in dir(real) if n != 'statvfs'])
        fake.stat, fake.path = real.stat, real.path
        with mock.patch.object(diskroom, 'os', fake), mock.patch('shutil.disk_usage', return_value=usage):
            space, slots, _dev, _gb = diskroom.reading('/')
        self.assertEqual((space, slots), (10.0, 100.0))

if __name__ == '__main__':
    unittest.main()
