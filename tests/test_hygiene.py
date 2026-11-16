"""Machine hygiene (tutti/hygiene.py, tutti/hygienecheck.py): yield flags expire, a hung WSL is restarted, disks are watched
and cleaned, never an image a project needs and never while the machine is held. Fake machines only: no shell reaches a
real machine or Docker."""
import json
import os
import subprocess
import time
import unittest
from argparse import Namespace
from types import SimpleNamespace
from unittest import mock

import tmpdirs
import yaml

from tutti import cli, config, hygiene, hygienecheck, routing
from tutti.routing import Probe

NOW = 1_800_000_000.0


def done(out='', rc=0):
    return subprocess.CompletedProcess([], rc, out, '')


def measure(flag=None, mtime=NOW - 3600, pid=None, host='pc', load='0.1 16', disks=(('/home/u', 500, 300), ('/mnt/c', 1000, 400))):
    lines = []
    if flag is not None:
        lines += [f'FLAG {mtime:.0f}', flag]
        if pid is not None:
            lines.append(f'PID {pid}')
    lines += [f'HOST {host}', f'LOAD {load}']
    lines += [f'DF {p} {size * 1048576:.0f} {free * 1048576:.0f}' for p, size, free in disks]
    return '\n'.join(lines) + '\n'


def make(machines, extra=None):
    d = tmpdirs.mkdtemp()
    raw = {'project': {'state': os.path.join(d, 'state'), 'worktrees': os.path.join(d, 'wt')}, 'machines': machines, **(extra or {})}
    path = os.path.join(d, 'tutti.config.yaml')
    with open(path, 'w', encoding='utf8') as f:
        yaml.safe_dump(raw, f)
    return config.load(path)


class Machine:
    """A fake machine: answers the measure, the image listing and the clean-up; records every script."""

    def __init__(self, meas, images='', freed_kb=0, rmi=(), down=False):
        self.down = down
        self.meas, self.images, self.freed, self.rmi, self.scripts = meas, images, freed_kb, rmi, []

    def __call__(self, m, script, timeout=120):
        self.scripts.append(script)
        if script.startswith('y='):
            return done('', 255) if self.down else done(self.meas)
        if script == hygiene.IMAGES_SCRIPT:
            return done(self.images)
        if 'FREED' in script:
            return done(''.join(f'RMI {r}\n' for r in self.rmi) + f'FREED {self.freed}\n')
        return done()


class Windows:
    """A fake Windows side of a WSL machine."""

    def __init__(self, hung=False, trace_bytes=0, freed=0):
        self.hung, self.trace, self.freed, self.calls = hung, trace_bytes, freed, []

    def __call__(self, m, command, timeout):
        self.calls.append(command)
        if command == 'echo ok':
            return done('ok\r\n')
        if command == 'wsl -e true' and self.hung:
            self.hung = False   # the first look times out; after the shutdown it answers
            raise subprocess.TimeoutExpired(command, timeout)
        if command == hygiene.TRACE_SIZE:
            return done(f'{self.trace}\r\n')
        if command == hygiene.TRACE_DELETE:
            self.trace = 0
            return done('done')
        if command.startswith('powershell') and command not in (hygiene.COMPACT,):
            return done(f'FREED {self.freed}\r\n')
        if command == hygiene.COMPACT:
            return done('SPARSE Ubuntu\r\n')
        return done()


def ctx(cfg, sh, win=None, questions=(), lowered=None):
    asked, offered = [], []
    lowered = [] if lowered is None else lowered
    c = SimpleNamespace(cfg=cfg, now=NOW, sh=sh, win=win or Windows(), questions=lambda cfg: list(questions),
                        offer=lambda cfg, key, q, why: offered.append((key, q)),
                        set_yield=lambda m, on, note='': lowered.append(m['name']) or 0,
                        ask=lambda cfg, key, q, why: asked.append((key, q)), remove_worktree=lambda p: False,
                        busy_dirs=lambda: set())
    return c, asked, offered, lowered


class Flags(unittest.TestCase):
    def test_a_new_flag_has_owner_reason_and_a_four_hour_expiry(self):
        f = hygiene.parse_flag(hygiene.flag_text('roradio', 'translation judge', now=NOW), NOW, NOW + 60)
        self.assertEqual((f['owner'], f['reason'], f['expires'] - f['at'], f['legacy']), ('roradio', 'translation judge', 4 * 3600, False))

    def test_an_old_plain_text_flag_expires_four_hours_after_it_was_written(self):
        f = hygiene.parse_flag('unload please', NOW - 5 * 3600, NOW)
        self.assertTrue(f['legacy'])
        self.assertIn('expired', hygiene.stale_flag(f, NOW, None, False))

    def test_renew_moves_the_expiry_and_keeps_the_owner(self):
        text = hygiene.renewed(hygiene.flag_text('ro', 'judge', now=NOW - 3 * 3600), NOW, 2, now=NOW)
        f = hygiene.parse_flag(text, NOW, NOW)
        self.assertEqual((f['owner'], f['expires']), ('ro', NOW + 2 * 3600))

    def test_a_live_flag_stays_and_a_gone_owner_only_matters_when_idle(self):
        f = hygiene.parse_flag(hygiene.flag_text('ro', 'x', pid=42, now=NOW), NOW, NOW)
        self.assertEqual(hygiene.stale_flag(f, NOW + 60, True, True), '')
        self.assertEqual(hygiene.stale_flag(f, NOW + 60, False, False), '')
        self.assertIn('process 42 is gone', hygiene.stale_flag(f, NOW + 60, False, True))


class Images(unittest.TestCase):
    LIST = ('IMAGES\n'
            'sha256:a\thcm-quality\tlocal\t2026-01-01 10:00:00 +0200 EET\n'
            'sha256:b\tregistry.example/hcm/pipeline\t<none>\t2026-01-01 10:00:00 +0200 EET\n'
            'sha256:c\t<none>\t<none>\t2026-01-01 10:00:00 +0200 EET\n'
            'sha256:d\told-tool\t1.0\t2026-01-01 10:00:00 +0200 EET\n'
            'sha256:e\tin-use\t2\t2026-01-01 10:00:00 +0200 EET\n'
            f'sha256:f\tfresh\t3\t{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - 3600))} +0000 UTC\n'
            'USED\nsha256:e\n')

    def test_old_unused_and_dangling_go_but_never_a_named_pinned_or_used_one(self):
        keep = ['hcm-quality:local', 'registry.example/hcm/pipeline']
        refs = [r for r, _ in hygiene.images_to_delete(self.LIST, keep, NOW)]
        self.assertEqual(refs, ['sha256:c', 'old-tool:1.0'])

    def test_without_the_keep_list_the_quality_image_would_go(self):
        self.assertIn('hcm-quality:local', [r for r, _ in hygiene.images_to_delete(self.LIST, [], NOW)])

    def test_every_image_the_config_names_is_kept(self):
        cfg = {'runner': {'image': 'hcm-quality:local'}, 'projects': {'p': {'tests': {'t': {'image': 'gdal:3'}}}},
               'selfheal': {'keep_images': ['worker@sha256:1']}, '_path': 'x'}
        self.assertEqual(hygiene.keep_images(cfg), ['gdal:3', 'hcm-quality:local', 'worker@sha256:1'])


class KeepConfig(unittest.TestCase):
    def test_keep_images_must_be_a_list(self):
        from tutti import selfheal
        self.assertTrue(any('keep_images' in e for e in selfheal.errors({'selfheal': {'keep_images': 'x'}})))
        self.assertFalse(any('keep_images' in e for e in selfheal.errors({'selfheal': {'keep_images': ['x']}})))


class Check(unittest.TestCase):
    def test_an_expired_flag_is_lowered_and_logged(self):
        cfg = make({'pc': {'transport': 'ssh', 'host': 'pc.example'}})
        c, _, _, lowered = ctx(cfg, Machine(measure('stuck', mtime=NOW - 15 * 3600)))
        lines = hygienecheck.check(c)
        self.assertEqual(lowered, ['pc'])
        self.assertTrue(any('expired' in x and 'lowered' in x for x in lines))
        self.assertIsNone(hygiene.read(cfg)['pc']['flag'])

    def test_a_live_flag_holds_the_clean_up_and_shows_its_owner(self):
        cfg = make({'pc': {'transport': 'ssh', 'host': 'pc.example'}})
        sh = Machine(measure(hygiene.flag_text('roradio', 'judge', now=NOW - 600), mtime=NOW - 600))
        c, _, _, lowered = ctx(cfg, sh)
        hygienecheck.check(c)
        e = hygiene.read(cfg)['pc']
        self.assertEqual((lowered, e['flag']['owner']), ([], 'roradio'))
        self.assertIn('clean-up waits', e['note'])
        self.assertNotIn(hygiene.IMAGES_SCRIPT, sh.scripts)

    def test_the_clean_up_reclaims_and_the_card_shows_the_week(self):
        cfg = make({'pc': {'transport': 'ssh', 'host': 'pc.example'}}, {'runner': {'image': 'hcm-quality:local'}})
        sh = Machine(measure(), images=Images.LIST, freed_kb=3 * 1048576, rmi=['old-tool:1.0'])
        c, *_ = ctx(cfg, sh)
        lines = hygienecheck.check(c)
        self.assertTrue(any('reclaimed 3.0 GB' in x for x in lines), lines)
        clean = [s for s in sh.scripts if 'FREED' in s][0]
        self.assertNotIn('hcm-quality', clean)
        self.assertEqual(hygiene.view(cfg, NOW)['pc']['reclaimed_week_gb'], 3.0)
        sh.scripts.clear()
        hygienecheck.check(c)   # done today: not again until tomorrow or under 20 %
        self.assertNotIn(hygiene.IMAGES_SCRIPT, sh.scripts)

    def test_a_lease_holds_the_clean_up(self):
        cfg = make({'pc': {'transport': 'ssh', 'host': 'pc.example'}})
        sh = Machine(measure())
        c, *_ = ctx(cfg, sh)
        with mock.patch.object(hygienecheck, 'leased', return_value={'pc': 1}):
            hygienecheck.check(c)
        self.assertIn('1 run(s) hold it', hygiene.read(cfg)['pc']['note'])

    def test_a_full_disk_asks_once_and_lists_the_biggest_items(self):
        cfg = make({'pc': {'transport': 'wsl', 'host': 'pc.example', 'wsl_helper': '/opt/run64.sh'}})
        sh = Machine(measure(disks=(('/home/u', 500, 300), ('/mnt/c', 1000, 5))))
        c, asked, offered, _ = ctx(cfg, sh, Windows(trace_bytes=42e9))
        hygienecheck.check(c)
        e = hygiene.read(cfg)['pc']
        self.assertEqual((e['disk']['level'], e['disk']['where']), ('stop', 'Windows C:'))
        self.assertIn('Remote Desktop traces on Windows: 42.0 GB', e['biggest'])
        self.assertEqual(len(asked), 1)
        self.assertEqual(offered[0][0], 'rdtrace:pc')

    def test_traces_go_only_after_the_owners_yes(self):
        cfg = make({'pc': {'transport': 'wsl', 'host': 'pc.example', 'wsl_helper': '/opt/run64.sh'}})
        win = Windows(trace_bytes=42e9)
        yes = [{'task': 'heal:rdtrace:pc', 'status': 'answered', 'answer': 'delete', 'answered': NOW - 60}]
        c, *_ = ctx(cfg, Machine(measure()), win, questions=yes)
        lines = hygienecheck.check(c)
        self.assertIn(hygiene.TRACE_DELETE, win.calls)
        self.assertTrue(any('Remote Desktop traces (42.0 GB, on your yes)' in x for x in lines), lines)

    def test_a_hung_wsl_is_restarted_when_nothing_holds_it(self):
        cfg = make({'pc': {'transport': 'wsl', 'host': 'pc.example', 'wsl_helper': '/opt/run64.sh'}})
        win = Windows(hung=True)
        c, *_ = ctx(cfg, Machine(measure()), win)
        lines = hygienecheck.check(c)
        self.assertIn('wsl --shutdown', win.calls)
        self.assertTrue(any('WSL hung' in x for x in lines))
        self.assertIn('restarted', hygiene.read(cfg)['pc']['wsl'])

    def test_a_hung_wsl_whose_look_fails_is_still_restarted(self):
        cfg = make({'pc': {'transport': 'wsl', 'host': 'pc.example', 'wsl_helper': '/opt/run64.sh'}})
        win = Windows(hung=True)
        c, *_ = ctx(cfg, Machine(measure(), down=True), win)
        hygienecheck.check(c)
        self.assertIn('wsl --shutdown', win.calls)

    def test_low_space_cleans_at_most_hourly(self):
        meas = hygiene.parse_measure(measure(disks=(('/home/u', 500, 50),)))
        self.assertFalse(hygienecheck.due({'cleaned_at': NOW - 600}, meas, NOW))
        self.assertTrue(hygienecheck.due({'cleaned_at': NOW - 4000}, meas, NOW))

    def test_a_projects_windows_folder_reaches_the_apk_clean_up(self):
        cfg = make({'pc': {'transport': 'wsl', 'host': 'pc.example', 'wsl_helper': '/opt/run64.sh'}},
                   {'projects': {'radio': {'windows': {'pc': 'C:\\Radio'}}}})
        win = Windows(freed=2e9)
        c, *_ = ctx(cfg, Machine(measure()), win)
        lines = hygienecheck.check(c)
        self.assertIn(hygiene.windows_clean(['C:\\Radio']), win.calls)
        self.assertTrue(any('reclaimed 2.0 GB' in x for x in lines), lines)

    def test_a_hung_wsl_is_left_while_a_flag_holds_it(self):
        cfg = make({'pc': {'transport': 'wsl', 'host': 'pc.example', 'wsl_helper': '/opt/run64.sh'}})
        win = Windows(hung=True)
        c, *_ = ctx(cfg, Machine(measure(hygiene.flag_text('ro', 'judge', now=NOW), mtime=NOW), down=True), win)
        hygiene.write(cfg, {'pc': {'flag': {'owner': 'ro', 'reason': 'judge', 'at': NOW, 'expires': NOW + 3600}}})   # last round's look
        hygienecheck.check(c)
        self.assertNotIn('wsl --shutdown', win.calls)
        self.assertIn('not restarted', hygiene.read(cfg)['pc']['wsl'])

    def test_an_unreachable_machine_is_skipped_and_a_phone_never_looked_at(self):
        cfg = make({'pc': {'transport': 'ssh', 'host': 'pc.example'}, 'phone': {'transport': 'android', 'host': 'p.example'}})
        sh = Machine('')
        sh.meas = ''
        c, *_ = ctx(cfg, lambda m, s, t=0: done('', 255) if m['name'] == 'pc' else self.fail('the phone was probed'))
        self.assertEqual(hygienecheck.check(c), [])


class Worktrees(unittest.TestCase):
    def test_an_old_run_worktree_is_a_candidate_unless_a_process_works_in_it(self):
        root = tmpdirs.mkdtemp()
        old = os.path.join(root, 'tutti-run-a')
        os.makedirs(os.path.join(old, 'sub'))
        os.makedirs(os.path.join(root, 'mine'))
        later = time.time() + 30 * 86400
        self.assertEqual(hygiene.old_worktrees(root, later), [old])
        self.assertEqual(hygiene.old_worktrees(root, later, {os.path.join(old, 'sub')}), [])
        self.assertEqual(hygiene.old_worktrees(root, time.time()), [])


class Created(unittest.TestCase):
    def test_both_time_formats_and_the_zero_time(self):
        a = hygiene.parse_created('2026-11-01 10:00:00 +0200 EET')
        b = hygiene.parse_created('2026-11-01T08:00:00.123456789Z')
        self.assertAlmostEqual(a, b, delta=1)
        self.assertIsNone(hygiene.parse_created('0001-01-01T00:00:00Z'))
        self.assertIsNone(hygiene.parse_created('soon'))

    def test_a_base_image_pulled_today_is_not_old(self):
        listing = 'IMAGES\nsha256:p\tpostgres\t16\t2026-01-01 10:00:00 +0000 UTC\nTAGS\n' \
            f'sha256:p\t{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 3600))}\n'
        self.assertEqual(hygiene.images_to_delete(listing, [], NOW), [])


class PowerShell(unittest.TestCase):
    def test_every_script_has_balanced_braces(self):
        import base64
        for c in (hygiene.windows_clean(['C:\\p']), hygiene.TRACE_SIZE, hygiene.TRACE_DELETE, hygiene.COMPACT):
            text = base64.b64decode(c.split()[-1]).decode('utf-16-le')
            self.assertEqual(text.count('{'), text.count('}'), text)


class MeasureScript(unittest.TestCase):
    def test_the_script_runs_here_and_parses(self):
        d = tmpdirs.mkdtemp()
        flag = os.path.join(d, 'yield')
        with open(flag, 'w', encoding='utf8') as f:
            f.write(hygiene.flag_text('me', 'test', pid=os.getpid()))
        r = subprocess.run(['bash', '-c', hygiene.measure_script({'yield_file': flag})], capture_output=True, text=True, check=False)
        m = hygiene.parse_measure(r.stdout)
        self.assertEqual((m['flag']['owner'], m['pid_alive']), ('me', True))
        self.assertTrue(m['disks'])

    def test_a_broken_reading_is_not_a_crash(self):
        self.assertEqual(hygiene.parse_measure('LOAD x y\nDF /h z z\n')['cores'], 1)


class Routing(unittest.TestCase):
    def test_a_nearly_full_disk_takes_nothing_new(self):
        self.assertIn('disk nearly full', routing.unfit({}, Probe(True, 2, 0.1, 8, disk_gb=5.0)))
        self.assertEqual(routing.unfit({}, Probe(True, 2, 0.1, 8, disk_gb=50.0)), '')

    def test_the_probe_line_carries_the_disk(self):
        self.assertEqual(routing.parse_probe('2 0.1 8 0 0 - 3.0 - 7.5').disk_gb, 7.5)
        self.assertIsNone(routing.parse_probe('2 0.1 8 0 0 - 3.0 - -').disk_gb)


class YieldCli(unittest.TestCase):
    def test_on_renew_and_status(self):
        d = tmpdirs.mkdtemp()
        flag = os.path.join(d, 'flag')
        cfg = make({'pc': {'transport': 'local', 'yield_file': flag}})
        a = Namespace(machine='pc', action='on', note='judge', unload=False, owner='ro', hours=1.0, pid=0)
        with mock.patch('builtins.print'):
            self.assertEqual(cli.cmd_yield(cfg, a), 0)
            with open(flag, encoding='utf8') as f:
                first = json.loads(f.read())
            self.assertEqual((first['owner'], first['reason']), ('ro', 'judge'))
            a.action, a.hours = 'renew', 5.0
            self.assertEqual(cli.cmd_yield(cfg, a), 0)
        with open(flag, encoding='utf8') as f:
            self.assertGreater(json.loads(f.read())['expires'], first['expires'])
        self.assertIn('ro: judge', cli.yield_line(json.dumps(first), first['at'] + 60))

    def test_renew_without_a_flag_says_how_to_raise_one(self):
        d = tmpdirs.mkdtemp()
        cfg = make({'pc': {'transport': 'local', 'yield_file': os.path.join(d, 'none')}})
        a = Namespace(machine='pc', action='renew', note='', unload=False, owner='', hours=4.0, pid=0)
        with mock.patch.object(cli, 'say') as say:
            self.assertEqual(cli.cmd_yield(cfg, a), 1)
        self.assertIn('tutti yield on', say.call_args[0][0])


if __name__ == '__main__':
    unittest.main()
