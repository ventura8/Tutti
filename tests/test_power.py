"""Measured machine power and local model tiers (tutti/power.py): the benchmark's line, the score and headroom, heavy shards
to the strongest free machine and light ones to the weaker, a weak busy machine never overloaded, a task's difficulty matched
to the weakest model tier that meets it, escalation after a failure. No machine or model server is called."""
import tempfile
import unittest
from unittest.mock import patch

from tutti import dispatch, fleet, power

PC = {'cores': 16, 'threads': 32, 'mem_gb': 64, 'cpu_mbs': 2000, 'disk_mbs': 5000, 'vram_gb': 32, 'npu': False}
NUC = {'cores': 16, 'threads': 22, 'mem_gb': 32, 'cpu_mbs': 1600, 'disk_mbs': 2000, 'vram_gb': 0, 'npu': True}
LAPTOP = {'cores': 8, 'threads': 16, 'mem_gb': 16, 'cpu_mbs': 1200, 'disk_mbs': 1500, 'vram_gb': 8, 'npu': False}


def row(name, bench, load=0.0, mem=20.0, **kw):
    return {'name': name, 'reachable': True, 'status': 'free', 'cores': (bench or {}).get('threads', 8), 'load': load,
            'mem_free_gb': mem, 'use_pct': 100, **kw}


def target(name, score, room=1.0):
    return {'id': f'machine:{name}', 'kind': 'machine', 'name': name, 'busy': False, 'blocked': '', 'power': score,
            'room': room, 'effective': power.effective(score, room)}


def shard(key, kind, minutes=0):
    return {'project': 'P', 'key': key, 'kind': kind, 'targets': ['machine'], 'machines': None, 'devices': None,
            'minutes': minutes, 'attempts': 0}


class Bench(unittest.TestCase):
    def test_the_benchmark_line_is_read_and_anything_else_is_none(self):
        self.assertEqual(power.parse_bench('noise\nB 8 16 15.5 1200 900 8 1\n'),
                         {'cores': 8, 'threads': 16, 'mem_gb': 15.5, 'cpu_mbs': 1200, 'disk_mbs': 900, 'vram_gb': 8, 'npu': True})
        self.assertIsNone(power.parse_bench('B 8 x 1 1 1 1 0'))
        self.assertIsNone(power.parse_bench(''))

    def test_the_pc_outscores_the_nuc_which_outscores_the_laptop(self):
        self.assertGreater(power.score(PC), power.score(NUC))
        self.assertGreater(power.score(NUC), power.score(LAPTOP))
        self.assertEqual(power.score(None), 0)
        self.assertEqual(sum(power.parts(PC).values()), power.score(PC))

    def test_headroom_falls_with_load_memory_heat_and_share(self):
        self.assertEqual(power.headroom(row('pc', PC)), 1.0)
        self.assertEqual(power.headroom(row('pc', PC, load=16)), 0.5)
        self.assertEqual(power.headroom(row('pc', PC, mem=1.0)), 0.0)
        self.assertEqual(power.headroom(row('pc', PC, heat={'cap_c': 80}, temp_c=85)), 0.5)
        self.assertEqual(power.headroom({**row('pc', PC), 'use_pct': 40}), 0.4)
        self.assertEqual(power.headroom({**row('pc', PC), 'status': 'offline'}), 0.0)
        self.assertEqual(power.headroom(None), 0.0)

    def test_stale_benchmarks_are_measured_once_in_the_background_and_rows_annotated(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch('tutti.power.host_power.get_power_mode', return_value={'mode': 'test', 'source': 'test'}),
        ):
            cfg = {'project': {'state': d}, 'machines': {'pc': {'transport': 'local'}, 'nuc': {'transport': 'local'}}}
            runs = []
            rows = [row('pc', PC), {**row('nuc', NUC), 'status': 'busy'}]
            power.after_probe(cfg, rows, 1000.0, measure_fn=lambda m: runs.append(m['name']) or PC, start=lambda fn: fn())
            self.assertEqual(runs, ['pc'])   # a busy machine waits: the benchmark neither slows nor misreads its work
            self.assertEqual(rows[0]['power']['score'], power.score(PC))
            self.assertEqual(rows[1]['power']['score'], 0)
            power.after_probe(cfg, rows, 2000.0, measure_fn=lambda m: runs.append(m['name']) or PC, start=lambda fn: fn())
            self.assertEqual(runs, ['pc'])   # fresh: not measured again until bench_days pass
            power.after_probe(cfg, rows, 1000.0 + 8 * 86400, measure_fn=lambda m: runs.append(m['name']) or PC,
                              start=lambda fn: fn())
            self.assertEqual(runs, ['pc', 'pc'])


class Dispatch(unittest.TestCase):
    def test_heavy_shards_go_to_the_strongest_machine_and_light_ones_to_the_weaker(self):
        tgts = [target('laptop', power.score(LAPTOP)), target('pc', power.score(PC)), target('nuc', power.score(NUC))]
        work = [shard('lint/1', 'lint'), shard('e2e/1', 'e2e'), shard('unit/1', 'unit')]
        p = dispatch.plan({}, {'leases': {}}, tgts, work, True)
        got = {t['name']: w['key'] for t, w in p['assign']}
        self.assertEqual(got['pc'], 'e2e/1')
        self.assertNotEqual(got['laptop'], 'e2e/1')
        self.assertIn('the strongest machine', dict((t['name'], t['why']) for t, _ in p['assign'])['pc'])

    def test_a_weak_machine_short_of_room_takes_no_heavy_shard(self):
        tgts = [target('pc', power.score(PC)), target('laptop', power.score(LAPTOP), room=0.1)]
        tgts[0]['busy'] = True
        p = dispatch.plan({}, {'leases': {}}, tgts, [shard('build/1', 'build')], True)
        self.assertEqual(p['assign'], [])
        self.assertIn('heavy shards wait for a stronger one', p['blocked']['machine:laptop'])
        # with room it takes the heavy shard rather than leave it waiting
        tgts[1] = target('laptop', power.score(LAPTOP))
        self.assertEqual(len(dispatch.plan({}, {'leases': {}}, tgts, [shard('build/1', 'build')], True)['assign']), 1)

    def test_heavy_by_kind_or_by_minutes(self):
        self.assertTrue(power.heavy(shard('x', 'e2e')))
        self.assertTrue(power.heavy(shard('x', 'unit', minutes=20)))
        self.assertFalse(power.heavy(shard('x', 'lint')))

    def test_unmeasured_machines_keep_the_old_order(self):
        tgts = [{**target('a', 0), 'effective': 0}, {**target('b', 0), 'effective': 0}]
        p = dispatch.plan({}, {'leases': {}}, tgts, [shard('e2e/1', 'e2e'), shard('lint/1', 'lint')], True)
        self.assertEqual([t['name'] for t, _ in p['assign']], ['a', 'b'])


STRONG = {'name': 'qwen3:32b', 'params_b': 32, 'tier': 'strong', 'kinds': list(fleet.KINDS), 'code': True, 'quant': 'Q4_K_M'}
MID = {'name': 'qwen3:8b', 'params_b': 8, 'tier': 'normal', 'kinds': list(fleet.TIERS['normal']['kinds']), 'code': True,
       'quant': 'Q4_K_M'}
SMALL = {'name': 'llama3.2:3b', 'params_b': 3, 'tier': 'small', 'kinds': list(fleet.TIERS['small']['kinds']), 'code': False,
         'quant': 'Q8_0'}


class Tiers(unittest.TestCase):
    def test_difficulty_by_size_kind_and_failures(self):
        self.assertEqual(power.difficulty({'title': 'fix x', 'size': 'S'}), 0)
        self.assertEqual(power.difficulty({'title': 'fix x', 'size': 'M'}), 1)
        self.assertEqual(power.difficulty({'title': 'review x', 'size': 'S'}), 1)
        self.assertEqual(power.difficulty({'title': 'fix x', 'size': 'S', 'attempts': [{'ok': False}]}), 1)
        self.assertEqual(power.difficulty({'title': 'fix x', 'size': 'L', 'attempts': [{'ok': False}] * 3}), 2)

    def test_tier_by_class_quantisation_and_measured_pass_rate(self):
        self.assertEqual(power.model_tier(STRONG)[0], 2)
        self.assertEqual(power.model_tier({**STRONG, 'quant': 'Q2_K'})[0], 1)
        self.assertEqual(power.model_tier(MID, {'qwen3:8b': (4, 5)}), (2, '8B normal, passed 4 of 5'))
        self.assertEqual(power.model_tier(MID, {'qwen3:8b': (0, 5)})[0], 0)
        self.assertEqual(power.model_tier(MID, {'qwen3:8b': (0, 2)})[0], 1)   # too few runs to judge
        self.assertEqual(power.pass_rates([{'attempts': [{'model': 'a', 'ok': True}, {'model': 'a', 'ok': False}, {'ok': True}]}]),
                         {'a': (1, 2)})

    def test_an_easy_task_gets_the_weakest_fit_and_a_failure_escalates(self):
        w = {'model': 'qwen3:32b', 'models': [STRONG, MID, SMALL]}
        docs = {'title': 'docs: readme typo', 'size': 'S', 'scope': ['README.md']}
        self.assertEqual(fleet.model_for(w, docs, {})['name'], 'llama3.2:3b')
        fix = {'title': 'fix the parser', 'size': 'S', 'scope': ['a.py']}
        self.assertEqual(fleet.model_for(w, fix, {})['name'], 'qwen3:8b')
        self.assertEqual(fleet.model_for(w, {**fix, 'attempts': [{'ok': False}]}, {})['name'], 'qwen3:8b')
        self.assertEqual(fleet.model_for(w, {**fix, 'attempts': [{'ok': False}] * 2}, {})['name'], 'qwen3:32b')
        self.assertEqual(fleet.model_for(w, {**fix, 'size': 'L'}, {})['name'], 'qwen3:32b')

    def test_order_keeps_the_strong_machine_free_for_hard_work(self):
        cfg = {'workers': {'pc-local': {'kind': 'ollama', 'model': 'qwen3:32b', 'machine': 'pc', 'models': [STRONG, MID]},
                           'laptop-local': {'kind': 'ollama', 'model': 'qwen3:8b', 'machine': 'laptop', 'models': [MID]}},
               'machines': {}}
        rows = [{'name': 'pc', 'status': 'free', 'power': {'score': power.score(PC)}},
                {'name': 'laptop', 'status': 'free', 'power': {'score': power.score(LAPTOP)}}]
        easy = {'title': 'fix a', 'size': 'S', 'scope': ['a.py']}
        self.assertEqual(fleet.order(cfg, ['pc-local', 'laptop-local'], easy, rows, {}), ['laptop-local', 'pc-local'])
        hard = {**easy, 'size': 'L'}
        self.assertEqual(fleet.order(cfg, ['pc-local', 'laptop-local'], hard, rows, {}), ['pc-local'])
        self.assertEqual(power.prefer(2, 0), 2)
        self.assertEqual(power.prefer(0, 2), 12)


if __name__ == '__main__':
    unittest.main()
