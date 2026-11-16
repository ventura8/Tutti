"""The Admin tab (docs/ADMIN.md): costs per row from the config and the electricity estimate, "not configured" rows that say
which setting fills them, totals and the forecast, the month history in the state folder, the controls list, and
GET /api/admin refused to a non-admin."""
import json
import os
import shutil
import subprocess
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import tmpdirs

from tutti import config, costs, dashboard

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc).timestamp()   # day 10 of a 30-day month


def cfg_with(folder, **c):
    cfg = config.load(os.path.join(config.ROOT, 'tutti.config.example.yaml'))
    cfg['project']['state'] = folder
    cfg['time'] = {'zone': 'UTC'}
    cfg['costs'] = {'currency': 'USD', 'subscriptions': {}, 'metered': {}, **c}
    return cfg


class CostsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdirs.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_a_subscription_metered_spend_and_electricity_are_priced_and_forecast(self):
        cfg = cfg_with(self.dir, subscriptions={'claude': {'monthly': 90}}, metered={'copilot': {'month_to_date': 3}})
        cards = [{'name': 'pc', 'measured': True, 'periods': {'month': {'cost': 2.0, 'kwh': 5}}}]
        cfg['energy']['currency'] = 'USD'
        snap = {'usage': {'accounts': [{'name': 'claude', 'used': 40, 'verdict': 'on pace'}]}, 'local_models': {'cards': cards}}
        v = costs.view(cfg, snap, NOW)
        by = {r['name']: r for r in v['rows']}
        self.assertEqual(by['claude']['mtd'], 30.0)
        self.assertEqual(by['claude']['forecast'], 90.0)
        self.assertEqual(by['claude']['usage']['used_pct'], 40)
        self.assertEqual(by['copilot']['forecast'], 9.0)
        self.assertEqual(by['electricity: pc']['forecast'], 6.0)
        self.assertEqual(v['totals']['mtd'], 35.0)
        self.assertEqual(v['history'][0]['month'], '2026-11')
        self.assertEqual(costs.load_history(cfg)['2026-11']['total'], 35.0)

    def test_an_account_without_a_fee_is_not_configured_and_says_which_setting(self):
        cfg = cfg_with(self.dir)
        v = costs.view(cfg, {}, NOW, write=False)
        cursor = next(r for r in v['rows'] if r['name'] == 'cursor')
        self.assertFalse(cursor['configured'])
        self.assertIn('costs.subscriptions.cursor.monthly', cursor['how'])
        self.assertGreater(v['totals']['unknown'], 0)
        self.assertEqual(v['totals']['mtd'], 0)

    def test_a_bad_fee_is_not_configured_and_an_unknown_currency_is_not_guessed(self):
        cfg = cfg_with(self.dir, subscriptions={'x': {'monthly': -5}, 'y': {'monthly': 10, 'currency': 'ZZZ'}})
        by = {r['name']: r for r in costs.view(cfg, {}, NOW, write=False)['rows']}
        self.assertFalse(by['x']['configured'])
        self.assertFalse(by['y']['configured'])

    def test_controls_link_to_where_each_lives(self):
        names = {c['name']: c['href'] for c in costs.controls({'failover': {'backup': 'nuc'}})}
        self.assertEqual(names['Backup main machine'], '#settings/machines')
        self.assertIn('Needs your input', names)


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdirs.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg = cfg_with(self.dir)

    def test_an_admin_gets_the_costs(self):
        c = SimpleNamespace(cfg=self.cfg, snapshot=lambda: {})
        code, body, _t, _c, _x = dashboard.get(c, dashboard.API_ADMIN, '', {})
        self.assertEqual(code, 200)
        self.assertIn('rows', json.loads(body))

    def test_a_viewer_is_refused(self):
        c = SimpleNamespace(cfg=self.cfg, snapshot=lambda: {}, authorize=lambda h, need: (403, 'admins only'))
        code, _b, _t, _c, _x = dashboard.get(c, dashboard.API_ADMIN, '', {})
        self.assertEqual(code, 403)


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class PageTest(unittest.TestCase):
    def test_rows_say_not_configured_and_the_headline_counts_them(self):
        with open(os.path.join(dashboard.WEB, 'admin.js'), encoding='utf8') as f:
            src = f.read()
        data = {'currency': 'USD', 'month': '2026-11', 'day': 10, 'days': 30, 'totals': {'mtd': 30, 'forecast': 90, 'unknown': 1},
                'rows': [{'name': 'claude', 'kind': 'subscription', 'configured': True, 'mtd': 30, 'forecast': 90, 'monthly': 90,
                          'usage': {'used_pct': 40, 'verdict': 'on pace'}},
                         {'name': 'cursor', 'kind': 'subscription', 'configured': False, 'how': 'set costs.subscriptions.cursor.monthly'}]}
        out = subprocess.run(['node', '-e', f'{src}\nconsole.log(JSON.stringify([adminCostRows({json.dumps(data)}), '
                              f'adminMeta({json.dumps(data)})]))'], capture_output=True, text=True, check=True)
        rows, meta = json.loads(out.stdout)
        self.assertIn('fee 90.00 USD/month', rows[0]['text'])
        self.assertIn('used 40%', rows[0]['text'])
        self.assertIn('not configured: set costs.subscriptions.cursor.monthly', rows[1]['text'])
        self.assertIn('1 not configured', meta)


if __name__ == '__main__':
    unittest.main()
