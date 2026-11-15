"""The backup main machine (docs/FAILOVER.md): election, failover, failback, no split brain, the state's budget, the watchdog,
the failover list members follow, and the best local model across the household (t80)."""
import json
import os
import time
import unittest

import tmpdirs
import yaml

from tutti import aiassist, config, coordinator, failover, failovercmd, statebudget, watchdog

CONFIG = {
    'project': {'state': 'STATE'},
    'machines': {
        'laptop': {'host': 'local'},
        'nuc': {'host': 'nuc.example', 'always_on': True, 'ups': True},
        'pc': {'host': 'pc.example'},
        'phone': {'host': 'phone.example', 'transport': 'android', 'runner': {'kind': 'shell'}},
    },
    'workers': {'w': {'kind': 'cli', 'command': ['true', '{prompt}']}},
}
OPTS = {**failover.DEFAULTS}


def make_cfg(me=None, backup=''):
    d = tmpdirs.mkdtemp()
    raw = json.loads(json.dumps(CONFIG).replace('STATE', os.path.join(d, 'state')))
    raw['failover'] = {'backup': backup}
    if me:
        raw['failover'].update(me=me, main='laptop')
    path = os.path.join(d, 'tutti.config.yaml')
    with open(path, 'w', encoding='utf8') as f:
        yaml.safe_dump(raw, f)
    return config.load(path)


class ElectionTest(unittest.TestCase):
    def test_the_always_on_machine_with_a_ups_is_elected_not_the_main_nor_a_phone(self):
        cfg = make_cfg()
        self.assertEqual(failover.main_of(cfg), 'laptop')
        self.assertEqual(failover.candidates(cfg), ['nuc', 'pc'])
        facts = {'nuc': {'up': True, 'uptime_s': 3600}, 'pc': {'up': True, 'uptime_s': 999999}}
        self.assertEqual(failover.elect(cfg, facts)[0], 'nuc')

    def test_most_uptime_wins_among_equals_and_a_down_machine_is_never_elected(self):
        cfg = make_cfg()
        cfg['machines']['nuc'].pop('ups')
        cfg['machines']['nuc'].pop('always_on')
        self.assertEqual(failover.elect(cfg, {'nuc': {'up': True, 'uptime_s': 10}, 'pc': {'up': True, 'uptime_s': 20}})[0], 'pc')
        self.assertEqual(failover.elect(cfg, {'nuc': {'up': False}, 'pc': {'up': True, 'uptime_s': 1}})[0], 'pc')
        backup, why = failover.elect(cfg, {'nuc': {'up': False}, 'pc': {'up': False}})
        self.assertIsNone(backup)
        self.assertIn('no candidate answers', why)

    def test_an_admin_pick_wins_and_none_turns_it_off(self):
        self.assertEqual(failover.elect(make_cfg(backup='pc'), {})[0], 'pc')
        self.assertIsNone(failover.elect(make_cfg(backup='none'), {})[0])

    def test_a_backup_that_is_not_a_machine_is_a_config_error_with_the_fix(self):
        cfg = make_cfg()
        cfg['failover']['backup'] = 'ghost'
        self.assertTrue(any('failover.backup names "ghost"' in e for e in config.validate(cfg)))
        with self.assertRaises(failover.FailoverError) as e:
            failovercmd.set_backup(cfg, 'phone')
        self.assertIn('it can be nuc, pc', str(e.exception))


class Cluster:
    """Two machines' term records and a network that can drop either way; time moves by hand."""

    def __init__(self):
        self.now = 1000.0
        self.main, self.backup = {}, {}
        self.link = True
        self.healthy_since = None

    def main_round(self):
        rec = self.main
        if rec.get('active', 'laptop') == 'laptop':
            beat = {'term': int(rec.get('term') or 0), 'from': 'laptop', 'lease_until': self.now + failover.lease_seconds(OPTS)}
            answer = None
            if self.link:
                self.backup, answer = failover.on_beat(self.backup, beat, self.now)
        else:
            answer = ({'ok': self.backup.get('active') == 'laptop', 'term': self.backup.get('term', 0),
                       'active': self.backup.get('active')} if self.link else None)
        new, act = failover.main_decide(rec, 'laptop', answer, self.now, self.healthy_since, OPTS)
        if act == 'catch-up':
            new['caught_up'] = True
        if act == 'hand-back' and self.link:
            self.backup, back = failover.on_hand_back(self.backup, 'laptop', new['term'])
            if back['ok']:
                new, act = {'term': back['term'], 'active': 'laptop', 'lease_until': None}, 'resumed'
        self.main = new
        return act

    def backup_round(self):
        act = failover.backup_decide(self.backup, 'nuc', self.now)
        if act == 'take-over':
            self.backup = failover.take_over(self.backup, 'nuc', self.now)
        return act

    def writers(self):
        out = []
        if failover.may_write({'machines': {'laptop': {'transport': 'local'}}, 'failover': {'me': 'laptop', 'main': 'laptop'}},
                              self.now, self.main or {'active': 'laptop', 'lease_until': 0}):
            out.append('laptop')
        if failover.may_write({'machines': {}, 'failover': {'me': 'nuc', 'main': 'laptop'}}, self.now, self.backup or {'active': 'x'}):
            out.append('nuc')
        return out

    def run(self, seconds, step=10):
        for _ in range(int(seconds // step)):
            self.main_round() if self.main is not None else None
            self.backup_round()
            assert len(self.writers()) <= 1, f'split brain at {self.now}: {self.writers()}'
            self.now += step


class FailoverTest(unittest.TestCase):
    def test_the_main_serves_while_the_backup_acknowledges_it(self):
        c = Cluster()
        c.run(120)
        self.assertEqual(c.writers(), ['laptop'])
        self.assertEqual(c.backup['active'], 'laptop')

    def test_the_backup_takes_over_after_the_main_is_silent_past_its_lease(self):
        c = Cluster()
        c.run(60)
        c.main = None   # the laptop is gone (lid closed, crashed)
        c.run(150)
        self.assertEqual(c.writers(), [])   # not before 3 min of silence and the margin
        c.run(90)
        self.assertEqual(c.writers(), ['nuc'])
        self.assertEqual(c.backup['term'], 1)

    def test_a_main_that_says_it_is_leaving_hands_over_at_once(self):
        c = Cluster()
        c.run(60)
        c.backup, _ = failover.on_beat(c.backup, {'term': 0, 'from': 'laptop', 'lease_until': 0, 'leaving': True}, c.now)
        c.main = None
        c.run(40)
        self.assertEqual(c.writers(), ['nuc'])

    def test_a_partition_never_gives_two_writers(self):
        c = Cluster()
        c.run(60)
        c.link = False   # both alive, the network between them cut
        c.run(600)       # Cluster.run asserts at most one writer every 10 s
        self.assertEqual(c.writers(), ['nuc'])
        self.assertEqual(c.main_round(), 'read-only')

    def test_failback_waits_for_healthy_minutes_and_catch_up_then_returns_to_the_main(self):
        c = Cluster()
        c.run(60)
        c.link = False
        c.run(300)
        c.link = True
        c.healthy_since = c.now
        self.assertEqual(c.main_round(), 'step-down')
        self.assertEqual(c.writers(), ['nuc'])
        c.run(120)
        self.assertEqual(c.writers(), ['nuc'])   # not healthy long enough yet
        c.run(300)
        self.assertEqual(c.writers(), ['laptop'])
        self.assertEqual(c.main['term'], 2)

    def test_a_stale_hand_back_is_refused(self):
        rec, ans = failover.on_hand_back({'term': 3, 'active': 'nuc'}, 'laptop', 2)
        self.assertFalse(ans['ok'])
        self.assertEqual(rec['active'], 'nuc')

    def test_an_old_heartbeat_from_a_lower_term_is_refused(self):
        rec, ans = failover.on_beat({'term': 2, 'active': 'nuc'}, {'term': 1, 'from': 'laptop', 'lease_until': 9e9}, 0)
        self.assertFalse(ans['ok'])
        self.assertEqual(rec['active'], 'nuc')


class EdgeTest(unittest.TestCase):
    def test_the_backup_round_starts_and_stops_its_dashboard(self):
        cfg = make_cfg(me='nuc')
        started, stopped = [], []
        failover.write(cfg, {'term': 0, 'active': 'laptop', 'beat_at': 0, 'lease_until': 0})
        act, line = failover.backup_round(cfg, now=1000, start=started.append, alive=lambda c: None, stop=stopped.append)
        self.assertEqual(act, 'serve')
        self.assertIn('took over', line)
        self.assertEqual(len(started), 1)
        failover.write(cfg, {'term': 2, 'active': 'laptop', 'beat_at': 1000, 'lease_until': 2000})
        act, _ = failover.backup_round(cfg, now=1100, start=started.append, alive=lambda c: 123, stop=stopped.append)
        self.assertEqual((act, len(stopped)), ('stand-by', 1))

    def test_the_backup_config_makes_the_backup_local_and_moves_the_paths(self):
        cfg = make_cfg()
        text = 'project:\n  state: /home/me/st\nmachines:\n  laptop: {host: local}\n  nuc: {host: nuc.example}\n'
        out = yaml.safe_load(failover.backup_config_text(cfg, text, 'nuc', '/home/me', '/home/other'))
        self.assertEqual(out['machines']['nuc']['host'], 'local')
        self.assertEqual(out['failover']['me'], 'nuc')
        self.assertTrue(out['project']['state'].startswith('/home/other/.tutti-failover'))

    def test_secrets_and_logs_never_go_but_the_backlog_and_runs_index_do(self):
        self.assertIn('*.log', failover.SYNC_EXCLUDE)
        self.assertIn('/runs/', failover.SYNC_EXCLUDE)
        self.assertNotIn('/runs.jsonl', failover.SYNC_EXCLUDE)
        self.assertFalse(any('backlog' in x or 'auth' in x for x in failover.SYNC_EXCLUDE))
        calls = []
        failover.rsync('a/', 'b:c/', run=lambda argv, **k: calls.append(argv) or type('R', (), {'returncode': 0, 'stdout': ''})())
        self.assertIn('--chmod=Du=rwx,go=,Fu=rw,go=', calls[0])   # mode 600 on the backup

    def test_an_unreachable_main_round_reports_read_only_in_plain_words(self):
        cfg = make_cfg()
        failover.write(cfg, {'term': 0, 'active': 'laptop', 'lease_until': 0})
        def fail(argv, **k):   # a fake ssh that cannot reach the backup
            return type('R', (), {'returncode': 255, 'stdout': '', 'stderr': ''})()
        act, line, _sent = failover.main_round(cfg, 'nuc', now=5000, run=fail)
        self.assertEqual(act, 'read-only')
        self.assertIn('start nuc', line)

    def test_the_discovery_list_names_the_main_then_the_backup(self):
        cfg = make_cfg(backup='nuc')
        cfg['machines']['laptop']['host'] = 'laptop.example'
        self.assertEqual(failover.discovery(cfg), ['laptop.example:4778', 'nuc.example:4778'])

    def test_a_member_follows_the_failover_list_when_the_coordinator_is_gone(self):
        m = {'coordinator': 'a:1', 'failover': ['a:1', 'b:2'], 'token': 't', 'user_id': 'u'}
        c = coordinator.Client(m)
        tried = []

        def post(path, body, key, user=''):
            tried.append(c.m['coordinator'])
            if c.m['coordinator'] == 'a:1':
                raise coordinator.Unreachable('down')
            return {'ok': True}
        c.post, c.remember = post, lambda m: None
        self.assertEqual(c.call('/x'), {'ok': True})
        self.assertEqual((tried, c.m['coordinator']), (['a:1', 'b:2'], 'b:2'))

    def test_all_coordinators_gone_raises_the_last_reason(self):
        c = coordinator.Client({'coordinator': 'a:1', 'token': 't', 'user_id': 'u'})
        c.post = lambda *a, **k: (_ for _ in ()).throw(coordinator.Unreachable('down'))
        with self.assertRaises(coordinator.Unreachable):
            c.call('/x')


class BudgetTest(unittest.TestCase):
    def test_the_size_and_the_biggest_items_are_measured_and_over_budget_is_a_problem(self):
        d = tmpdirs.mkdtemp()
        with open(os.path.join(d, 'backlog.json'), 'wb') as f:
            f.write(b'x' * 3 * 1024 * 1024)
        lines, problems = statebudget.report(d, {**statebudget.DEFAULTS, 'budget_mb': 50})
        self.assertIn('3.0 MB of a 50 MB budget', lines[0])
        self.assertEqual(problems, [])
        _lines, problems = statebudget.report(d, {**statebudget.DEFAULTS, 'budget_mb': 1})
        self.assertIn('tutti state prune', problems[0])

    def test_prune_caps_runs_and_logs_and_drops_old_screenshots_but_keeps_the_backlog(self):
        d = tmpdirs.mkdtemp()
        os.makedirs(os.path.join(d, 'runs'))
        old = time.time() - 30 * 86400
        for i in range(5):
            p = os.path.join(d, 'runs', f'r{i}')
            open(p, 'w').close()
            os.utime(p, (old + i, old + i) if i < 2 else None)
        for n, data in (('shot.png', b'p'), ('backlog.json', b'{}'), ('big.log', b'line\n' * 400000)):
            with open(os.path.join(d, n), 'wb') as f:
                f.write(data)
        os.utime(os.path.join(d, 'shot.png'), (old, old))
        _freed, lines = statebudget.prune(d, {**statebudget.DEFAULTS, 'runs_keep': 2, 'log_mb': 1})
        self.assertEqual(len(os.listdir(os.path.join(d, 'runs'))), 2)
        self.assertFalse(os.path.exists(os.path.join(d, 'shot.png')))
        self.assertTrue(os.path.exists(os.path.join(d, 'backlog.json')))
        self.assertLessEqual(os.path.getsize(os.path.join(d, 'big.log')), 1024 * 1024)
        self.assertTrue(lines)


class WatchdogTest(unittest.TestCase):
    def test_a_corrupt_file_comes_back_from_its_last_good_snapshot(self):
        d = tmpdirs.mkdtemp()
        p = os.path.join(d, 'backlog.json')
        with open(p, 'w', encoding='utf8') as f:
            json.dump({'items': [1]}, f)
        self.assertEqual(watchdog.round_files(d), [])
        with open(p, 'w', encoding='utf8') as f:
            f.write('{"items": [1')
        lines = watchdog.round_files(d)
        self.assertIn('put back', lines[0])
        with open(p, encoding='utf8') as f:
            self.assertEqual(json.load(f), {'items': [1]})

    def test_a_corrupt_file_without_a_snapshot_is_reported_not_guessed(self):
        d = tmpdirs.mkdtemp()
        with open(os.path.join(d, 'x.json'), 'w', encoding='utf8') as f:
            f.write('{')
        self.assertIn('no good snapshot', watchdog.round_files(d)[0])

    def test_a_crashed_dashboard_is_started_only_where_tutti_serves(self):
        started = []
        self.assertEqual(watchdog.round_services(True, lambda: False, lambda: started.append(1)),
                         ['the dashboard did not answer: started again'])
        self.assertEqual(watchdog.round_services(False, lambda: False, lambda: started.append(1)), [])
        self.assertEqual(watchdog.round_services(True, lambda: True, lambda: started.append(1)), [])
        self.assertEqual(started, [1])


class LocalModelTest(unittest.TestCase):
    CFG = {'ai_assist': {'enabled': True, 'local': True}, 'machines': {}, 'workers': {
        'laptop': {'kind': 'ollama', 'host': 'l', 'model': 'qwen3.5:9b'},
        'nuc': {'kind': 'ollama', 'host': 'n', 'model': 'deepseek-coder-v2:latest', 'size_b': 16},
        'pc': {'kind': 'ollama', 'host': 'p', 'model': 'qwen3.8:27b-q8_0'}}}

    def setUp(self):
        self.CFG = {**self.CFG, 'project': {'state': tmpdirs.mkdtemp()}}

    def test_the_largest_model_comes_first_whichever_machine_it_is_on(self):
        self.assertEqual(aiassist.local_workers(self.CFG), ['pc', 'nuc', 'laptop'])

    def test_a_busy_or_down_machine_is_skipped_quickly_then_the_next(self):
        answer, who = aiassist.ask_local(self.CFG, 'p', available=lambda w: (w['host'] != 'p', ''),
                                         generate=lambda c, w, p, **k: f'from {w["host"]}', busy_fn=lambda w: w['host'] == 'n')
        self.assertEqual((answer, who), ('from l', 'laptop'))

    def test_with_every_local_model_busy_it_goes_online(self):
        answer, who = aiassist.ask_local(self.CFG, 'p', available=lambda w: (True, ''), generate=None, busy_fn=lambda w: True,
                                         online=lambda c, p: ('online answer', 'grok'))
        self.assertEqual((answer, who), ('online answer', 'grok'))

    def test_busy_means_another_model_is_loaded(self):
        w = {'host': 'x', 'model': 'qwen3:27b'}
        self.assertTrue(aiassist.busy(w, {'models': [{'name': 'llama3:8b'}]}))
        self.assertFalse(aiassist.busy(w, {'models': [{'name': 'qwen3:27b'}]}))
        self.assertFalse(aiassist.busy(w, {'models': []}))


if __name__ == '__main__':
    unittest.main()
