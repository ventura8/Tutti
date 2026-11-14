"""Time zones: detection on Linux, Windows and macOS, daylight saving in Europe/Bucharest, a household member in another
zone, a remote machine's zone, and no bare UTC in what a person reads."""
import os
import re
import unittest
from datetime import timedelta
from unittest import mock

from tutti import coordinator, dispatch, owner, pacing, planner, readers, tz

BUC = 'Europe/Bucharest'


def T(s):
    return pacing.parse_time(s)


def fake_read(files):
    return lambda path: files.get(path, '')


def no_run(cmd):
    return ''


def answers(program, text):
    """A fake command runner: `program` prints `text`, every other command prints nothing."""
    return lambda cmd: text if cmd[0] == program else ''


class Detect(unittest.TestCase):
    def test_linux_reads_etc_timezone_then_the_localtime_link_then_timedatectl(self):
        self.assertEqual(tz.detect({}, fake_read({'/etc/timezone': 'Europe/Bucharest\n'}), no_run, lambda p: p, 'posix'),
                         (BUC, '/etc/timezone'))
        self.assertEqual(tz.detect({}, fake_read({}), no_run, lambda p: '/usr/share/zoneinfo/Europe/Bucharest', 'posix'),
                         (BUC, '/etc/localtime'))
        self.assertEqual(tz.detect({}, fake_read({}), answers('timedatectl', 'Europe/Bucharest\n'), lambda p: p, 'posix'),
                         (BUC, 'timedatectl'))

    def test_tz_wins_and_a_bad_name_is_skipped(self):
        self.assertEqual(tz.detect({'TZ': ':America/New_York'}, fake_read({'/etc/timezone': BUC}), no_run, lambda p: p, 'posix'),
                         ('America/New_York', 'TZ'))
        self.assertEqual(tz.detect({'TZ': 'Mars/Olympus'}, fake_read({'/etc/timezone': BUC}), no_run, lambda p: p, 'posix')[0], BUC)
        self.assertEqual(tz.detect({}, fake_read({}), no_run, lambda p: p, 'posix'), ('', ''))

    def test_windows_maps_tzutil_to_iana(self):
        self.assertEqual(tz.detect({}, fake_read({}), answers('tzutil', 'GTB Standard Time\r\n'), lambda p: p, 'nt'),
                         (BUC, 'tzutil /g'))
        self.assertEqual(tz.detect({}, fake_read({}), answers('tzutil', 'Pacific Standard Time'), lambda p: p, 'nt')[0],
                         'America/Los_Angeles')

    def test_macos_reads_systemsetup(self):
        self.assertEqual(tz.detect({}, fake_read({}), answers('systemsetup', 'Time Zone: America/New_York\n'), lambda p: p, 'posix'),
                         ('America/New_York', 'systemsetup'))

    def test_a_remote_machine_prints_its_zone(self):
        self.assertEqual(tz.parse_remote('/usr/share/zoneinfo/Asia/Tokyo'), 'Asia/Tokyo')
        self.assertEqual(tz.parse_remote('../usr/share/zoneinfo/Europe/Bucharest\n'), BUC)
        self.assertEqual(tz.parse_remote(''), '')
        self.assertIn('/etc/localtime', tz.remote_command())

    def test_config_auto_and_override(self):
        with mock.patch('tutti.tz.host_zone', return_value=BUC):
            self.assertEqual(tz.zone_name({'time': {'zone': 'auto'}}), BUC)
            self.assertEqual(tz.zone_name({'time': {'zone': 'Asia/Tokyo'}}), 'Asia/Tokyo')
        with mock.patch('tutti.tz.host_zone', return_value=''):
            self.assertEqual(tz.zone_name({'time': {'zone': 'auto'}}, browser='America/Chicago'), 'America/Chicago')
            self.assertEqual(tz.zone_name({'time': {'zone': 'auto'}}), 'UTC')
        self.assertEqual(tz.errors({'time': {'zone': 'auto'}}), [])
        self.assertIn('time.zone', tz.errors({'time': {'zone': 'Nowhere/Land'}})[0])


class Bucharest(unittest.TestCase):
    """DST: 29 Mar 2026 03:00 -> 04:00 (UTC+2 -> UTC+3), 25 Oct 2026 04:00 -> 03:00 (UTC+3 -> UTC+2)."""

    def test_offsets_and_how_a_time_is_shown(self):
        self.assertEqual(tz.offset_hours(BUC, T('2026-03-29T00:30Z')), 2)
        self.assertEqual(tz.offset_hours(BUC, T('2026-03-29T01:30Z')), 3)
        self.assertEqual(tz.offset_hours(BUC, T('2026-10-25T00:30Z')), 3)
        self.assertEqual(tz.offset_hours(BUC, T('2026-10-25T01:30Z')), 2)
        self.assertEqual(tz.show(T('2026-11-15T00:00Z'), BUC), 'Sun 15 Nov 02:00 (UTC+2)')
        self.assertEqual(tz.show(T('2026-07-15T00:00Z'), BUC), 'Wed 15 Jul 03:00 (UTC+3)')
        self.assertEqual(tz.show(T('2026-12-09T19:00Z'), BUC), 'Wed 09 Dec 21:00 (UTC+2)')
        self.assertEqual(tz.label(5.5), 'UTC+5:30')

    def test_owner_hours_follow_the_clock_across_both_changes(self):
        sched = {**owner.DEFAULT, 'window': '01-14', 'days': list(owner.DAYS[:5])}
        prof = owner.profile_of(sched, [], BUC)
        ctx = {'reserve': prof['reserve'], 'offset': BUC}
        # Fri 27 Mar (UTC+2): 01:00 local is 23:00Z; Mon 30 Mar (UTC+3): 01:00 local is 22:00Z
        self.assertEqual(owner.reserve_until(ctx, T('2026-03-26T22:00Z'), T('2026-03-26T23:00Z')), 0)
        self.assertGreater(owner.reserve_until(ctx, T('2026-03-26T23:00Z'), T('2026-03-27T00:00Z')), 0)
        self.assertGreater(owner.reserve_until(ctx, T('2026-03-29T22:00Z'), T('2026-03-29T23:00Z')), 0)
        # October: Fri 23 (UTC+3) starts at 22:00Z, Mon 26 (UTC+2) at 23:00Z
        self.assertGreater(owner.reserve_until(ctx, T('2026-10-22T22:00Z'), T('2026-10-22T23:00Z')), 0)
        self.assertEqual(owner.reserve_until(ctx, T('2026-10-25T22:00Z'), T('2026-10-25T23:00Z')), 0)
        self.assertGreater(owner.reserve_until(ctx, T('2026-10-25T23:00Z'), T('2026-10-26T00:00Z')), 0)

    def _readings(self, start, days, zone):
        """Hourly readings: the workers' 1 point per hour, and the owner's 10 more from 09:00 to 10:00 local on weekdays."""
        out, used, t = [], 0.0, T(start)
        for _ in range(days * 24):
            out.append({'at': t.isoformat(), 'used': used})
            lt = tz.local(t, zone)
            used += 1 + (10 if lt.hour == 9 and lt.weekday() < 5 else 0)
            t += timedelta(hours=1)
        return out

    def test_learned_buckets_stay_on_the_local_hour_across_dst(self):
        for start in ('2026-03-16T00:00Z', '2026-10-12T00:00Z'):   # two weeks before and two after each change
            profile, _, _ = owner.learn(self._readings(start, 28, BUC), BUC)
            for d in range(5):
                self.assertEqual(max(range(24), key=lambda h, d=d: profile[d][h]), 9, (start, d))
        # the old fixed offset smeared it: summer's 09:00 landed in the 08:00 bucket
        profile, _, _ = owner.learn(self._readings('2026-03-30T00:00Z', 7, BUC), 2.0)
        self.assertEqual(max(range(24), key=lambda h: profile[0][h]), 8)

    def test_vacation_and_away_end_at_local_midnight(self):
        mode, until = owner.effective('vacation-until-2026-07-01', [[0] * 24] * 7, [], T('2026-06-20T12:00Z'), BUC, 3)
        self.assertEqual((mode, until), ('vacation', T('2026-06-30T21:00Z')))
        mode, until = owner.effective('away', [[0] * 24] * 7, [], T('2026-12-01T12:00Z'), BUC, 3)
        self.assertEqual(until, T('2026-12-01T22:00Z'))

    def test_quiet_hours_are_local(self):
        self.assertTrue(planner.quiet(T('2026-01-06T21:30Z'), ['22:00-06:00'], BUC))    # 23:30 in Bucharest
        self.assertFalse(planner.quiet(T('2026-01-06T19:30Z'), ['22:00-06:00'], BUC))
        self.assertTrue(planner.quiet(T('2026-07-06T19:30Z'), ['22:00-06:00'], BUC))    # 22:30 in summer

    def test_resets_stated_in_utc_or_a_zone(self):
        self.assertEqual(pacing.parse_time('2026-11-15 02:00 Europe/Bucharest'), T('2026-11-15T00:00Z'))
        self.assertEqual(pacing.parse_time('2026-11-15T02:00+02:00'), T('2026-11-15T00:00Z'))
        self.assertEqual(pacing.parse_time('2026-11-15T00:00Z'), T('2026-11-15T00:00Z'))
        now = T('2026-11-14T10:00Z')
        self.assertEqual(readers.clock_reset('resets 3pm (Europe/Bucharest)', now), T('2026-11-14T13:00Z'))
        self.assertEqual(readers.clock_reset('resets 3pm (UTC)', now), T('2026-11-14T15:00Z'))
        with mock.patch('tutti.tz.host_zone', return_value=BUC), mock.patch.dict(tz._CHOSEN, {'zone': 'auto'}):
            self.assertEqual(readers.clock_reset('resets at 15:30', now), T('2026-11-14T13:30Z'))


class Household(unittest.TestCase):
    def test_a_member_in_another_zone_is_weighed_by_their_own_hours(self):
        profile = [[1.0 if h == 9 else 0.0 for h in range(24)] for _ in range(7)]
        weigh = coordinator.Service.weight_at(None, T('2026-01-06T14:30Z').timestamp())
        self.assertEqual(weigh({'weight': 1, 'profile': profile, 'zone': 'America/New_York'}), 2.0)   # 09:30 in New York
        self.assertEqual(weigh({'weight': 1, 'profile': profile, 'zone': BUC}), 1.0)                  # 16:30 in Bucharest
        self.assertEqual(weigh({'weight': 1, 'profile': profile, 'utc_offset': -5}), 2.0)              # an older member's beat

    def test_a_machine_keeps_quiet_hours_in_its_own_zone(self):
        cfg = {'dispatch': {'quiet_hours': ['22:00-06:00']}}
        now = T('2026-01-06T14:30Z').timestamp()
        self.assertIn('Asia/Tokyo time', dispatch.quiet_now(cfg, now, 'Asia/Tokyo'))   # 23:30 in Tokyo
        with mock.patch('tutti.tz.host_zone', return_value=BUC), mock.patch.dict(tz._CHOSEN, {'zone': 'auto'}):
            self.assertEqual(dispatch.quiet_now(cfg, now, ''), '')                        # 16:30 here


class NoBareUtc(unittest.TestCase):
    """A time a person reads is local with its offset ('21:00 (UTC+2)'), never a bare '21:00 UTC'."""
    BARE = re.compile(r"""(%H:%M|%H|\d\d:\d\d|\})['"]?\s?UTC\b(?!\+|-|\))|\((UTC)\)['"]""")

    def test_no_user_facing_string_prints_bare_utc(self):
        root = os.path.join(os.path.dirname(__file__), '..', 'tutti')
        hits = []
        for name in sorted(os.listdir(root)):
            if name.endswith('.py'):
                with open(os.path.join(root, name), encoding='utf8') as f:
                    hits += [f'{name}:{i}: {line.strip()}' for i, line in enumerate(f, 1) if self.BARE.search(line)]
        self.assertEqual(hits, [])

    def test_the_planner_text_is_local(self):
        with mock.patch('tutti.tz.host_zone', return_value=BUC), mock.patch.dict(tz._CHOSEN, {'zone': 'auto'}):
            text = planner._at(T('2026-12-09T19:00Z'), T('2026-12-01T00:00Z'))
            self.assertEqual(text, 'Wed 09 Dec 21:00 (UTC+2)')
            self.assertNotIn(' UTC', pacing.report({'c': {'window': '7d', 'resets': '2026-01-03T02:00Z'}}, [], T('2026-01-06T12:00Z')))


if __name__ == '__main__':
    unittest.main()
