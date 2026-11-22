"""Backups (t246): the config and the state folder in one dated archive, verified as made, the newest kept; a damaged
archive is caught; restore checks first, keeps the old state aside and never writes outside the state folder."""
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from tutti import backup, backupcmd, dashboard


class Backup(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = d.name
        self.state = os.path.join(d.name, 'state')
        for rel, text in (('backlog.json', '{"tasks": []}'), ('chat/a.json', '{}'), ('patches/wt-t1/x.py', 'x'),
                          ('tests/t.log', 'x'), ('backlog.json.lock', '')):
            os.makedirs(os.path.dirname(os.path.join(self.state, rel)), exist_ok=True)
            with open(os.path.join(self.state, rel), 'w') as f:
                f.write(text)
        self.cfg_path = os.path.join(d.name, 'config.yaml')
        with open(self.cfg_path, 'w') as f:
            f.write('machines: {}\n')
        self.cfg = {'_path': self.cfg_path, 'project': {'state': self.state},
                    'backup': {'dest': os.path.join(d.name, 'backups'), 'keep': 2}}

    def test_what_goes_in_and_what_stays_out(self):
        self.assertEqual(backup.members(self.state), ['backlog.json', 'chat/a.json'])   # no worktrees, test leftovers, locks

    def test_a_backup_is_made_verified_and_only_the_newest_kept(self):
        for i, t in enumerate((1_800_000_000, 1_800_000_100, 1_800_000_200)):
            rec = backup.run(self.cfg, now=t + i)
            self.assertTrue(rec['ok'], rec['problems'])
        names = [b['name'] for b in backup.listing(self.cfg)]
        self.assertEqual(len(names), 2)
        with tarfile.open(backup.listing(self.cfg)[0]['path']) as tar:
            self.assertEqual(sorted(tar.getnames()), ['MANIFEST.json', 'config.yaml', 'state/backlog.json', 'state/backup.json',
                                                      'state/chat/a.json'])   # the last backup's record goes in too
        self.assertTrue(backup.load(self.cfg)['ok'])
        self.assertEqual(backup.prune(['b1', 'b3', 'b2'], 1), ['b2', 'b1'])

    def test_a_damaged_or_foreign_archive_is_caught(self):
        good = backup.create(self.cfg, now=1_800_000_000)
        bad = os.path.join(self.root, 'bad.tar.gz')
        with tarfile.open(good) as src, tarfile.open(bad, 'w:gz') as out:
            for m in src:
                data = src.extractfile(m).read() if m.isfile() else None
                if m.name == 'state/backlog.json':
                    data = b'{"tasks": ["changed"]}'
                    m.size = len(data)
                out.addfile(m, io.BytesIO(data) if data is not None else None)
        self.assertIn('state/backlog.json differs from its checksum', backup.verify(bad))
        with open(bad, 'wb') as f:
            f.write(b'not an archive')
        self.assertIn('cannot be read', backup.verify(bad)[0])
        with self.assertRaisesRegex(backup.BackupError, 'did not verify'):
            backup.restore(self.cfg, bad)
        self.assertTrue(os.path.isfile(os.path.join(self.state, 'backlog.json')))   # nothing changed

    def test_restore_keeps_the_old_state_aside_and_puts_the_backup_back(self):
        path = backup.create(self.cfg, now=1_800_000_000)
        with open(os.path.join(self.state, 'backlog.json'), 'w') as f:
            f.write('{"tasks": ["new"]}')
        aside = backup.restore(self.cfg, path, now=1_800_000_500)
        with open(os.path.join(self.state, 'backlog.json')) as f:
            self.assertEqual(json.load(f), {'tasks': []})
        with open(os.path.join(aside, 'backlog.json')) as f:
            self.assertEqual(json.load(f), {'tasks': ['new']})
        shutil.rmtree(aside)

    def test_an_unwritable_destination_says_what_to_do(self):
        with open(os.path.join(self.root, 'afile'), 'w') as f:
            f.write('x')
        cfg = {**self.cfg, 'backup': {'dest': os.path.join(self.root, 'afile', 'sub')}}
        rec = backup.run(cfg, now=1_800_000_000)
        self.assertFalse(rec['ok'])
        self.assertIn('set backup.dest to a folder you may write', rec['problems'][0])

    def test_the_command_restores_only_with_yes(self):
        backup.run(self.cfg, now=1_800_000_000)
        lines = []
        self.assertEqual(backupcmd.cmd_backup(self.cfg, mock.Mock(action='restore', file=None, yes=False), lines.append), 2)
        self.assertIn('Stop the dashboard first', lines[-1])
        self.assertEqual(backupcmd.cmd_backup(self.cfg, mock.Mock(action='verify', file=None, yes=False), lines.append), 0)
        self.assertIn('sound', lines[-1])

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_card_line(self):
        with open(os.path.join(dashboard.WEB, 'discovered.js'), encoding='utf8') as f:
            src = f.read()
        stub = ('function el(tag, attrs, ...kids) { return { tag, attrs: attrs || {}, kids: kids.flat() }; }\n'
                "function text(n) { return typeof n === 'string' ? n : n ? "
                "[(n.attrs || {}).text || '', ...(n.kids || []).map(text)].join(' ') : ''; }\n")
        js = (f'{stub}\n{src}\nconsole.log(JSON.stringify([text(backupNode(null)), '
              'text(backupNode({at: 1800000000, ok: false, size: 0, problems: ["disk full"]}))]));')
        none, failed = json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout)
        self.assertIn('none yet', none)
        self.assertIn('disk full', failed)


if __name__ == '__main__':
    unittest.main()
