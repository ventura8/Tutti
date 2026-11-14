"""Local models and their estimated energy cost (tutti/energy.py, tutti/web/energy.js): power parsed from fake nvidia-smi, rocm,
xpu-smi, intel_gpu_top and hwmon output, the energy integration and buckets, currency detection, the price table and the card."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from tutti import config, dashboard, energy

ZONE_TAB = '# comment\nRO\t+4426+02606\tEurope/Bucharest\nDE\t+5230+01322\tEurope/Berlin\nUS\t+404251-0740023\tAmerica/New_York\n'
NVIDIA = 'NVIDIA GeForce RTX 3080, 20.10, 3, 320.00\nNVIDIA GeForce RTX 5090, 412.50, 97, 600.00\n'   # the busy one second
ROCM = json.dumps({'card0': {'Average Graphics Package Power (W)': '183.0', 'GPU use (%)': '88'},
                   'card1': {'Current Socket Graphics Package Power (W)': '12.0', 'GPU use (%)': '1'}})
XPU = 'Timestamp, DeviceId, GPU Utilization (%), GPU Power (W)\n06:14:46.000,    0, 64.00, 31.50\n'
INTEL = ('[\n{"period": {"duration": 1000.0}, "power": {"GPU": 0.0, "Package": 0.0, "unit": "W"}, '
         '"engines": {"Render/3D": {"busy": 0.0}}},\n'
         '{"period": {"duration": 1000.0}, "power": {"GPU": 18.25, "Package": 30.1, "unit": "W"}, '
         '"engines": {"Render/3D": {"busy": 12.5}, "Compute": {"busy": 71.0}}}')
SNAP_A = 't 1000000000000\nhwmon xe 5000000 -\nrapl 100000000\n'
SNAP_B = 't 1001000000000\nhwmon xe 25000000 -\nrapl 125000000\n'


def script_output(**parts):
    return '\n'.join(f'== {k}\n{v}' for k, v in parts.items()) + '\n== end\n'


class Power(unittest.TestCase):
    def test_nvidia_sums_watts_and_takes_the_busiest_gpu(self):
        r = energy.parse_nvidia(NVIDIA)
        self.assertAlmostEqual(r['gpu_w'], 432.6)
        self.assertEqual((r['util'], r['gpu'], r['limit_w'], r['source']), (97, 'NVIDIA GeForce RTX 5090', 600, 'nvidia-smi'))

    def test_nvidia_without_a_power_reading_and_garbage(self):
        r = energy.parse_nvidia('NVIDIA GeForce RTX 3080 Laptop GPU, [N/A], 40, [N/A]')
        self.assertIsNone(r['gpu_w'])
        self.assertEqual(r['util'], 40)
        self.assertIsNone(energy.parse_nvidia('nvidia-smi: command not found'))

    def test_rocm(self):
        r = energy.parse_rocm(ROCM)
        self.assertEqual((r['gpu_w'], r['util'], r['source']), (195.0, 88, 'rocm-smi'))
        self.assertIsNone(energy.parse_rocm('not json'))
        self.assertIsNone(energy.parse_rocm('{"card0": {"GPU use (%)": "3"}}'))

    def test_intel_xpu_and_intel_gpu_top(self):
        self.assertEqual(energy.parse_xpu(XPU)['gpu_w'], 31.5)
        self.assertEqual(energy.parse_xpu(XPU)['util'], 64)
        self.assertIsNone(energy.parse_xpu('Timestamp, DeviceId\n'))
        r = energy.parse_intel(INTEL)   # the first (all-zero) sample is skipped
        self.assertEqual((r['gpu_w'], r['util'], r['source']), (18.25, 71.0, 'intel_gpu_top'))
        self.assertIsNone(energy.parse_intel(''))

    def test_hwmon_and_rapl_counters(self):
        r = energy.parse_counters(SNAP_A, SNAP_B)
        self.assertAlmostEqual(r['gpu_w'], 20.0)
        self.assertAlmostEqual(r['cpu_w'], 25.0)
        self.assertEqual(energy.parse_counters('rapl -\n', 'rapl -\n'), {})

    def test_the_whole_script_output_prefers_a_tool_then_the_counters(self):
        r = energy.parse_power(script_output(nvidia='', rocm='', xpu='', intel='', a=SNAP_A, b=SNAP_B, ps='{"models": []}', tags='oops'))
        self.assertEqual((round(r['gpu_w']), round(r['cpu_w']), r['source']), (20, 25, 'hwmon xe'))
        self.assertEqual((r['ps'], r['tags']), ({'models': []}, None))
        r = energy.parse_power(script_output(nvidia=NVIDIA, a='', b=''))
        self.assertEqual((r['source'], r['cpu_w']), ('nvidia-smi', None))
        r = energy.parse_power(script_output(nvidia='', a='', b=''))
        self.assertIsNone(r['gpu_w'])

    def test_estimate_from_tdp_when_nothing_is_read(self):
        w, parts, measured = energy.watts({'gpu': 'NVIDIA GeForce RTX 5090'}, {'base_w': 40}, busy=True)
        self.assertEqual(measured, [])
        self.assertAlmostEqual(parts['gpu'], 575 * (0.1 + 0.9 * 0.9), places=1)
        self.assertEqual(parts['base'], 40)
        idle, _, _ = energy.watts({'gpu': 'Arc'}, {'cpu_tdp_w': 28}, busy=False)
        self.assertAlmostEqual(idle, 2.8 + 2.8 + 30)
        cpu_model, parts, _ = energy.watts({}, {'gpu_tdp_w': 50}, busy=True, on_gpu=False)
        self.assertEqual((parts['gpu'], parts['cpu']), (5.0, 58.5))   # a model on the CPU: most of the CPU's TDP
        w, _, measured = energy.watts({'gpu_w': 300, 'cpu_w': 45, 'source': 'nvidia-smi'}, {}, busy=True)
        self.assertEqual((w, measured), (375, ['GPU (nvidia-smi)', 'CPU (RAPL)']))


class Energy(unittest.TestCase):
    def test_integrate_holds_each_sample_until_the_next(self):
        self.assertAlmostEqual(energy.integrate([(0, 100), (1800, 300)], 0, 3600), 50 + 150)
        self.assertAlmostEqual(energy.integrate([(1800, 200)], 0, 3600), 200)   # before the first sample it holds too
        self.assertAlmostEqual(energy.integrate([], 0, 7200, fallback_w=50), 100)
        self.assertEqual(energy.integrate([(0, 100)], 10, 10), 0)

    def test_record_buckets_busy_and_idle_by_local_hour_with_a_gap_cap(self):
        utc = time.gmtime
        store = energy.record({}, 'pc', 1000.0, 400, 'busy', 60, local=utc)   # the first sample only starts the clock
        self.assertEqual(store['pc']['days'], {})
        energy.record(store, 'pc', 1030.0, 400, 'busy', 60, local=utc)
        energy.record(store, 'pc', 2030.0, 100, 'idle', 60, local=utc)       # a long gap counts as max_gap only
        energy.record(store, 'pc', 2060.0, 999, 'off', 60, local=utc)        # off: nothing counted
        b = store['pc']['days']['1970-01-01']
        self.assertEqual((b['busy_s'], b['idle_s']), (30, 60))
        self.assertAlmostEqual(b['busy_wh'][0], 400 * 30 / 3600)
        self.assertAlmostEqual(b['idle_wh'][0], 100 * 60 / 3600)
        energy.record(store, 'pc', 2060.0 + 50 * 86400, 1, 'busy', 60, local=utc)
        self.assertNotIn('1970-01-01', store['pc']['days'])                   # old days are dropped

    def test_cost_follows_a_time_of_day_tariff(self):
        t = {'price': 1.0, 'currency': 'RON', 'periods': [{'hours': '23-07', 'price': 0.5}]}
        self.assertEqual((energy.price_at(t, 2), energy.price_at(t, 12), energy.price_at(t, 23)), (0.5, 1.0, 0.5))
        day = {'busy_s': 7200, 'idle_s': 0, 'busy_wh': [1000.0] + [0.0] * 11 + [1000.0] + [0.0] * 11, 'idle_wh': [0.0] * 24}
        self.assertEqual(energy._period_sums({'d': day}, t, ['d', 'missing']), {'busy_s': 7200, 'idle_s': 0, 'kwh': 2.0, 'cost': 1.5})

    def test_cheap_hours_only_when_asked(self):
        t = {'price': 1.0, 'currency': 'RON', 'periods': [{'hours': '23-07', 'price': 0.5}]}
        self.assertEqual(energy.cheap_now({'energy': {}}, t, 12), '')
        on = {'energy': {'prefer_cheap_hours': True}}
        self.assertIn('from 23:00', energy.cheap_now(on, t, 12))
        self.assertEqual(energy.cheap_now(on, t, 3), '')
        self.assertEqual(energy.cheap_now(on, {**t, 'periods': []}, 12), '')


class Currency(unittest.TestCase):
    def test_locales(self):
        for lang, country, cur in (('ro_RO.UTF-8', 'RO', 'RON'), ('en_US.UTF-8', 'US', 'USD'), ('de_DE.UTF-8', 'DE', 'EUR')):
            t = energy.tariff({'energy': {}}, env={'LANG': lang}, zone='', zone_tab='')
            self.assertEqual((t['country'], t['currency']), (country, cur), lang)
            self.assertTrue(t['estimated'])

    def test_the_time_zone_beats_an_english_locale(self):
        env = {'LANG': 'en_US.UTF-8', 'LC_MONETARY': 'en_GB.UTF-8'}
        t = energy.tariff({'energy': {}}, env=env, zone='Europe/Bucharest', zone_tab=ZONE_TAB)
        self.assertEqual((t['country'], t['currency'], t['price']), ('RO', 'RON', 1.40))
        self.assertIn('Europe/Bucharest', t['how'])

    def test_settings_override_and_unknown_country(self):
        cfg = {'energy': {'country': 'de', 'currency': 'eur', 'price_per_kwh': 0.31}}
        t = energy.tariff(cfg, env={}, zone='', zone_tab='')
        self.assertEqual((t['country'], t['currency'], t['price'], t['estimated']), ('DE', 'EUR', 0.31, False))
        t = energy.tariff({'energy': {}}, env={'LANG': 'C'}, zone='', zone_tab='')
        self.assertEqual((t['country'], t['currency'], t['price']), ('', 'EUR', 0.29))
        self.assertIn('nothing', t['how'])
        t = energy.tariff({'energy': {'currency': 'usd'}}, env={'LANG': 'ro_RO.UTF-8'}, zone='', zone_tab='')
        self.assertEqual((t['currency'], t['price']), ('USD', round(1.40 / 4.38, 3)))   # converted, never 1.40 USD
        t = energy.tariff({'energy': {'currency': 'xyz'}}, env={'LANG': 'ro_RO.UTF-8'}, zone='', zone_tab='')
        self.assertEqual((t['currency'], t['price']), ('EUR', 0.29))


class Prices(unittest.TestCase):
    def test_the_table_is_complete_and_sane(self):
        for country, (cur, price) in energy.PRICES.items():
            self.assertRegex(country, r'^[A-Z]{2}$')
            self.assertIn(cur, energy.PER_USD, f'{country}: no exchange rate for the cloud comparison')
            self.assertGreater(price, 0)
        self.assertIn('2025', energy.PRICE_SOURCE)
        for _, pin, pout in energy.CLOUD.values():
            self.assertGreater(pout, pin)

    def test_cloud_cost_in_the_users_currency(self):
        t = {'currency': 'RON'}
        c = energy.cloud_cost({'energy': {}}, t, 1_000_000, 1_000_000)
        self.assertEqual((c['model'], c['usd'], c['cost']), ('Claude Sonnet', 18.0, round(18 * 4.38, 2)))
        self.assertIsNone(energy.cloud_cost({'energy': {}}, {'currency': 'XYZ'}, 1, 1)['cost'])

    def test_config_errors_name_the_key_and_fix(self):
        errs = energy.errors({'energy': {'price_per_kwh': -1, 'compare_with': 'nope', 'tariff': [{'hours': 'night'}],
                                         'machines': {'pc': {'base_w': 'lots'}}}})
        self.assertEqual(len(errs), 4)
        errs = energy.errors({'energy': {'machines': {'nas': {'ollama': '1.2.3.4:1; rm -rf ~'}}}})
        self.assertEqual(len(errs), 1)
        self.assertIn('host:port', errs[0])
        self.assertNotIn('rm -rf', energy.power_script('1.2.3.4:1; rm -rf ~'))
        self.assertIn('curl -s --max-time 3 http://127.0.0.1:11434/api/ps;', energy.power_script('127.0.0.1:11434'))
        self.assertTrue(all(e.startswith('energy.') for e in errs))
        self.assertEqual(energy.errors({'energy': config.DEFAULTS['energy']}), [])


class Cards(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.cfg = config.normalise({'project': {'state': self.dir}, 'machines': {'pc': {'host': 'pc.example'}},
                                     'workers': {'big': {'kind': 'ollama', 'host': '127.0.0.1:1', 'model': 'm:1', 'machine': 'pc'},
                                                 'small': {'kind': 'ollama', 'host': '127.0.0.1:2', 'model': 'm:2'}},
                                     'energy': {'machines': {'nas': {'ollama': '127.0.0.1:11434'}}}})
        self.cfg['_path'] = os.path.join(self.dir, 'tutti.config.yaml')

    def test_hosts_come_from_ollama_workers_and_the_energy_machines(self):
        h = energy.hosts(self.cfg)
        self.assertEqual(h['pc'], {'workers': ['big'], 'ollama': '127.0.0.1:1', 'on': '127.0.0.1:11434'})   # its own curl too
        self.assertEqual(h['this computer']['workers'], ['small'])
        self.assertEqual(h['nas'], {'workers': [], 'ollama': None, 'on': '127.0.0.1:11434'})

    def test_a_run_is_logged_counted_and_priced(self):
        now = time.time()
        energy.log_run(self.cfg, 'big', now - 600, now - 300, {'eval_count': 5000, 'prompt_eval_count': 1000, 'model': 'm:1'})
        reading = {'gpu': 'NVIDIA GeForce RTX 5090', 'gpu_w': 420.0, 'util': 95, 'source': 'nvidia-smi', 'cpu_w': None,
                   'ps': {'models': [{'name': 'm:1', 'size_vram': 5}]}, 'tags': {'models': [{'name': 'm:1'}, {'name': 'a:2'}]}}
        store = {}
        energy.record(store, 'pc', now - 600, 450, 'busy', 600)
        energy.record(store, 'pc', now - 300, 450, 'busy', 600)
        t = {'country': 'RO', 'how': 'test', 'currency': 'RON', 'price': 1.4, 'estimated': True, 'source': 's', 'periods': []}
        v = energy.view(self.cfg, {'pc': reading}, [{'name': 'pc', 'paused': True, 'reason': 'lent to its owner'}], now, t=t, store=store)
        c = next(x for x in v['cards'] if x['name'] == 'pc')
        self.assertEqual((c['installed'], c['loaded'], c['status']), (['a:2', 'm:1'], [{'name': 'm:1', 'on_gpu': True}], 'yielding'))
        self.assertEqual(c['measured'], ['GPU (nvidia-smi)'])
        self.assertEqual((c['periods']['month']['jobs'], c['periods']['month']['tokens']), (1, 5000))
        self.assertAlmostEqual(c['periods']['week']['kwh'], 450 * 300 / 3600 / 1000, places=2)
        self.assertEqual(c['per_busy_hour'], round(0.45 * 1.4, 2))
        self.assertEqual(c['periods']['month']['cloud']['model'], 'Claude Sonnet')
        nas = next(x for x in v['cards'] if x['name'] == 'nas')
        self.assertEqual((nas['reachable'], nas['status']), (False, 'offline'))
        self.assertIn('did not answer', nas['reason'])

    def test_a_run_in_flight_makes_the_machine_busy_and_samples_count_it(self):
        with open(os.path.join(self.dir, 'local-run-1-2'), 'w', encoding='utf8') as f:
            f.write(f'big {time.time()}')
        readings = {'pc': {'gpu_w': 300.0, 'source': 'nvidia-smi', 'ps': {'models': []}}}
        energy.sample(self.cfg, readings, 1000.0, 60)
        store = energy.sample(self.cfg, readings, 1030.0, 60)
        day = next(iter(store['pc']['days'].values()))
        self.assertEqual(day['busy_s'], 30)
        self.assertTrue(energy.view(self.cfg, readings, [], 1030.0)['cards'][0]['busy_now'])

    def test_the_collector_reads_power_and_the_snapshot_shows_the_category(self):
        def power(machine, on):
            return {'gpu': 'Arc', 'gpu_w': None, 'util': None, 'source': '', 'cpu_w': None, 'ps': None, 'tags': None}
        c = dashboard.Collector(self.cfg, probe_fn=lambda m: __import__('tutti.routing').routing.Probe(), available_fn=lambda w: (True, ''),
                                reader=type('R', (), {'read_all': lambda *a: {}, 'scan_jobs': lambda *a: []})(),
                                activity_fn=lambda *a: {}, prs_fn=lambda cfg: {}, power_fn=power,
                                ollama_fn=lambda host, path: {'models': [{'name': 'm:1'}]})
        c.refresh()
        lm = c.snapshot()['local_models']
        self.assertEqual(sorted(x['name'] for x in lm['cards']), ['nas', 'pc', 'this computer'])
        self.assertIsNone(lm['error'])
        self.assertTrue(os.path.exists(os.path.join(self.dir, energy.BUSY_FILE)))


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class Render(unittest.TestCase):
    def js(self, expr):
        with open(os.path.join(dashboard.WEB, 'energy.js'), encoding='utf8') as f:
            src = f.read()
        out = subprocess.run(['node', '-e', f'{src}\nconsole.log(JSON.stringify({expr}));'], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    CARD = {'name': 'pc', 'gpu': 'NVIDIA GeForce RTX 5090', 'status': 'thermal', 'reason': 'CPU 91 C', 'busy_now': False, 'reachable': True,
            'loaded': [{'name': 'qwen:27b', 'on_gpu': True}, {'name': 'gemma:12b', 'on_gpu': False}], 'installed': ['qwen:27b'],
            'watts': 512, 'util': 97, 'parts': {'gpu': 450, 'cpu': 32, 'base': 30}, 'measured': ['GPU (nvidia-smi)'],
            'per_job': 0.042, 'per_busy_hour': 0.72,
            'periods': {'today': {'busy_s': 5400, 'jobs': 3, 'tokens': 9000, 'kwh': 0.77, 'cost': 1.08},
                        'week': {'busy_s': 30, 'jobs': 0, 'tokens': 0, 'kwh': 0, 'cost': 0},
                        'month': {'busy_s': 9000, 'jobs': 7, 'tokens': 20000, 'kwh': 1.5, 'cost': 2.1,
                                  'cloud': {'model': 'Claude Sonnet', 'cost': 1.37, 'usd': 0.31}}}}

    def test_the_card_text(self):
        v = self.js(f'tuttiLocalCard({json.dumps(self.CARD)}, {{currency: "RON"}})')
        self.assertEqual((v['status'], v['tone']), ('thermal', 'bad'))
        self.assertEqual(v['models'][0], 'Loaded: qwen:27b, gemma:12b (CPU)')
        self.assertIn('512 W', v['power'])
        self.assertIn('at 97 %', v['power'])
        self.assertEqual(v['periods'][0], {'label': 'Today', 'busy': '1.5 h', 'jobs': 3, 'tokens': 9000, 'kwh': '0.77', 'cost': '1.1 RON'})
        self.assertEqual(v['periods'][1]['busy'], '30 s')
        self.assertEqual((v['perJob'], v['perHour']), ('0.04 RON', '0.72 RON'))
        self.assertIn('Claude Sonnet would cost about 1.4 RON', v['cloud'])
        self.assertTrue(v['measured'].startswith('Measured: GPU (nvidia-smi)'))

    def test_an_unread_machine_and_no_tokens(self):
        card = {**self.CARD, 'reachable': False, 'reason': 'its power and models were not read', 'busy_now': True,
                'measured': [], 'per_job': None, 'loaded': [], 'installed': [],
                'periods': {'month': {'tokens': 0, 'cloud': {'model': 'x', 'cost': 0}}}}
        v = self.js(f'tuttiLocalCard({json.dumps(card)}, {{currency: "RON"}})')
        self.assertEqual((v['status'], v['tone']), ('busy', 'accent'))
        self.assertEqual(v['models'], ['Loaded: none', 'Installed: not read'])
        self.assertTrue(v['measured'].startswith('Estimated'))
        self.assertEqual(v['perJob'], 'no job this month')
        self.assertIn('No tokens', v['cloud'])
        t = {'price': 1.4, 'currency': 'RON', 'estimated': True, 'country': 'RO', 'how': 'the time zone Europe/Bucharest', 'periods': []}
        line = self.js(f'tuttiTariffLine({json.dumps(t)})')
        self.assertEqual(line, 'Electricity 1.4 RON/kWh (estimated, typical for RO, found from the time zone Europe/Bucharest)')


if __name__ == '__main__':
    unittest.main()
