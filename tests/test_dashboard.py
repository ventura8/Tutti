"""The dashboard: pacing and panic state, jobs from the state folder, machines and workers, the summary and the HTTP server."""
import gzip
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from tutti import cli, config, dashboard, pacing, routing, state, waiting

T = pacing.parse_time
NOW = T('2026-01-06T02:00Z')
ACC = {'window': '7d', 'resets': '2026-01-03T02:00Z', 'lead_hours': 1}


def reading(at, used, account='claude'):
    return {'at': at, 'account': account, 'used': used, 'note': ''}


def cfg_of(raw, folder):
    raw = {'project': {'worktrees': folder, 'state': os.path.join(folder, 'state')}, **raw}
    cfg = config.normalise(raw)
    cfg['_path'] = os.path.join(folder, 'tutti.config.yaml')
    return cfg


class AccountStateTest(unittest.TestCase):
    def test_on_pace_with_readings_of_this_window_only(self):
        entries = [reading('2026-01-02T00:00Z', 90), reading('2026-01-04T00:00Z', 10), reading('2026-01-06T00:00Z', 42)]
        s = pacing.account_state('claude', ACC, entries, NOW)
        self.assertEqual((s['state'], s['verdict'], s['used'], s['panic']), ('ok', 'on pace', 42.0, False))
        self.assertEqual([r['used'] for r in s['readings']], [10.0, 42.0])
        self.assertEqual(s['target'], T('2026-01-10T01:00Z').isoformat())

    def test_panic_before_the_target_says_what_to_do(self):
        s = pacing.account_state('claude', {**ACC, 'panic_at': 90}, [reading('2026-01-06T00:00Z', 93)], NOW)
        self.assertTrue(s['panic'])
        self.assertIn('nothing new on claude', s['action'])

    def test_no_panic_once_the_target_time_has_passed(self):
        self.assertFalse(pacing.panic(99, T('2026-01-10T01:30Z'), T('2026-01-10T01:00Z')))
        self.assertTrue(pacing.panic(99, T('2026-01-09T00:00Z'), T('2026-01-10T01:00Z')))
        self.assertFalse(pacing.panic(50, T('2026-01-09T00:00Z'), T('2026-01-10T01:00Z')))

    def test_missing_reset_and_missing_reading_name_the_fix(self):
        s = pacing.account_state('agy', {'source': 'the account page'}, [], NOW)
        self.assertEqual(s['state'], 'no reset')
        self.assertIn('accounts.agy.resets', s['action'])
        s = pacing.account_state('claude', ACC, [], NOW)
        self.assertEqual(s['state'], 'no reading')
        self.assertIn('tutti usage log claude', s['action'])

    def test_report_shows_panic(self):
        text = pacing.report({'claude': ACC}, [reading('2026-01-06T00:00Z', 97)], NOW)
        self.assertIn('panic', text)


class ConfigTest(unittest.TestCase):
    def test_dashboard_defaults_and_bad_values(self):
        cfg = config.normalise({})
        self.assertEqual(cfg['dashboard']['port'], 4777)
        self.assertEqual(config.validate(cfg), [])
        errs = config.validate(config.normalise({'dashboard': {'port': 'x', 'stall_minutes': 0}, 'accounts': {'a': {'panic_at': 150}}}))
        self.assertTrue(any('dashboard.port' in e for e in errs))
        self.assertTrue(any('dashboard.stall_minutes' in e for e in errs))
        self.assertTrue(any('accounts.a.panic_at' in e for e in errs))
        nan = config.normalise({'dashboard': {'probe_seconds': float('nan')}})
        self.assertTrue(any('dashboard.probe_seconds' in e for e in config.validate(nan)))
        inf = config.normalise({'dashboard': {'refresh_seconds': float('inf')}})
        self.assertTrue(any('dashboard.refresh_seconds' in e for e in config.validate(inf)))
        self.assertTrue(any('write it as a mapping' in e for e in config.validate(config.normalise({'dashboard': None}))))


class OffAccountCardsTest(unittest.TestCase):
    def test_off_accounts_are_displayed_but_never_added_to_routing(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = cfg_of({'workers': {'worker': {}, 'stopped': {'paused': {'by': 'owner', 'at': 1, 'note': 'break'}}},
                          'accounts': {'active': {'worker': 'worker'}, 'paused': {'paused': {'by': 'owner', 'at': 1, 'note': 'rest'}},
                                       'disabled': {'enabled': False}, 'linked': {'worker': 'stopped'}}}, folder)
            usage = state.usage_state(cfg, NOW)
            self.assertEqual([a['name'] for a in usage['accounts']], ['active'])
            cards = {a['name']: a for a in usage['off_accounts']}
            self.assertEqual(set(cards), {'paused', 'disabled', 'linked'})
            self.assertEqual(cards['paused']['status'], 'paused')
            self.assertIn('rest', cards['paused']['reason'])
            self.assertEqual(cards['disabled']['status'], 'disabled')
            self.assertEqual(cards['linked']['status'], 'paused')
            self.assertIn('worker stopped', cards['linked']['reason'])
            self.assertEqual(state.worker_accounts(cfg), ['active'])
            self.assertNotIn('stopped', cfg['workers'])

    def test_all_off_still_provides_display_cards(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = cfg_of({'accounts': {'disabled': {'enabled': False}}}, folder)
            usage = state.usage_state(cfg, NOW)
            self.assertEqual(usage['accounts'], [])
            self.assertEqual([a['name'] for a in usage['off_accounts']], ['disabled'])
            self.assertEqual(state.worker_accounts(cfg), [])


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.cfg = cfg_of({'dashboard': {'stall_minutes': 1}}, self.dir)
        self.state = os.path.join(self.dir, 'state')
        os.makedirs(os.path.join(self.state, 'quiet'))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text, age=0):
        path = os.path.join(self.state, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf8') as f:
            f.write(text)
        t = time.time() - age
        os.utime(path, (t, t))
        return path

    def test_statuses_titles_and_tails(self):
        self.write('quiet/100-11.log', 'a\n\x1b[32mok\x1b[0m\n')
        self.write('quiet/100-11.log.rc', '0\n')
        self.write('quiet/100-11.log.cmd', 'npm test\n')
        self.write('ship-200-22.log', 'boom\n')
        self.write('ship-200-22.log.rc', '2\n')
        self.write('quiet/300-33.log', 'working\n', age=600)   # alive, quiet for 10 min: stalled
        self.write('quiet/400-44.log', 'fresh\n')               # alive, fresh: running
        self.write('quiet/50-55.log', 'old\n')                  # gone, no result recorded: ended
        self.write('ship-500-66-2.log', 'checked\n')           # a numbered check whose ship process lives on: its result wins
        self.write('ship-500-66-2.log.rc', '0\n')
        jobs = state.jobs_state(self.cfg, time.time(), is_alive=lambda pid: int(pid) in (33, 44, 66))
        by = {j['id']: j for j in jobs['items']}
        self.assertEqual((by['ship:ship-500-66-2.log']['status'], by['ship:ship-500-66-2.log']['pid']), ('done', 66))
        self.assertEqual(by['quiet:100-11.log']['status'], 'done')
        self.assertEqual(by['quiet:100-11.log']['title'], 'npm test')
        self.assertEqual(by['quiet:100-11.log']['tail'], ['a', 'ok'])
        self.assertEqual((by['ship:ship-200-22.log']['status'], by['ship:ship-200-22.log']['rc']), ('failed', 2))
        self.assertEqual(by['quiet:300-33.log']['status'], 'stalled')
        self.assertEqual(by['quiet:400-44.log']['status'], 'running')
        self.assertEqual(by['quiet:50-55.log']['status'], 'ended')
        self.assertEqual(jobs['items'][0]['status'], 'stalled')   # what needs a look comes first

    def test_waits_tunnels_and_local_model_jobs(self):
        log = self.write('watched.log', 'x\n', age=600)
        self.write('waits/77.json', json.dumps({'kind': 'file', 'target': '/x.done', 'watch': log, 'stall': 0}))
        self.write('waits/78.json', json.dumps({'kind': 'pid', 'target': '1'}))   # its waiter is gone: not shown
        self.write('llm-busy-79', '')
        self.write('tunnel-pc.pid', '80\n')
        self.write('tunnel-pc.log', 'up\n')
        jobs = state.jobs_state(self.cfg, time.time(), is_alive=lambda pid: int(pid) in (77, 79, 80))
        by = {j['id']: j for j in jobs['items']}
        self.assertEqual(by['wait:77']['status'], 'stalled')
        self.assertNotIn('wait:78', by)
        self.assertEqual(by['llm:79']['status'], 'running')
        self.assertEqual(by['tunnel:pc']['tail'], ['up'])

    def test_an_empty_state_folder_has_no_jobs(self):
        self.assertEqual(state.jobs_state(cfg_of({}, self.dir), time.time())['items'], [])

    def test_register_and_unregister_a_wait(self):
        path = waiting.register(os.path.join(self.state, 'waits'), {'kind': 'file', 'target': '/x'}, pid=5)
        self.assertTrue(path.endswith('5.json'))
        waiting.unregister(path)
        waiting.unregister(path)   # twice is harmless
        self.assertFalse(os.path.exists(path))


class MachinesWorkersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_of({'machines': {'a': {'host': 'a.example'}, 'b': {'host': 'b.example'}, 'c': {'host': 'c.example'},
                                        'd': {'host': 'd.example'}},
                           'routing': {'order': ['a', 'b']},
                           'workers': {'cursor': {'kind': 'cli', 'command': ['cursor-agent', '{prompt}']},
                                       'small': {'kind': 'ollama', 'host': 'h:1', 'model': 'm'}},
                           'accounts': {'cursor': {}}}, self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_machine_statuses(self):
        probes = {'a.example': routing.Probe(True, 2, 1.0, 8, False), 'b.example': routing.Probe(True, 0, 1.0, 8, False),
                  'c.example': routing.Probe(True, 2, 0.5, 8, True), 'd.example': routing.Probe()}
        ms = {m['name']: m for m in state.machines_state(self.cfg, lambda m: probes[m['host']])}
        self.assertEqual([ms[n]['status'] for n in 'abcd'], ['free', 'busy', 'paused', 'offline'])
        self.assertEqual((ms['a']['rank'], ms['c']['rank']), (1, None))
        self.assertEqual((ms['b']['busy'], ms['d']['free']), (2, None))

    def test_machine_environments_name_their_physical_card(self):
        self.cfg['machines']['pc'] = config.machine_of(self.cfg, 'pc', {'host': 'pc.example'})
        self.cfg['machines']['pc-desktop'] = config.machine_of(self.cfg, 'pc-desktop', {'host': 'pc.example', 'dedicated': True})
        probes = {'pc.example': routing.Probe(True, 2, 0.1, 8)}
        ms = {m['name']: m for m in state.machines_state(self.cfg, lambda m: probes.get(m['host'], routing.Probe()))}
        self.assertEqual(ms['pc']['physical'], 'pc')
        self.assertEqual(ms['pc-desktop']['physical'], 'pc')

    def test_workers_and_known_agents(self):
        w = state.workers_state(self.cfg, lambda x: (x['kind'] == 'ollama', 'note'), which=lambda c: c == 'claude' and '/bin/claude')
        by = {x['name']: x for x in w['items']}
        self.assertEqual((by['cursor']['ready'], by['small']['ready'], by['cursor']['account']), (False, True, 'cursor'))
        agents = {a['command']: a for a in w['agents']}
        self.assertEqual((agents['claude']['installed'], agents['claude']['configured']), (True, False))
        self.assertEqual((agents['cursor-agent']['installed'], agents['cursor-agent']['configured']), (False, True))
        self.assertTrue(agents['ollama']['installed'])   # its server answers, even with no binary here
        self.assertFalse(agents['copilot']['installed'])


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        folder = cls.tmp.name
        log = os.path.join(folder, 'usage.jsonl')
        now = pacing.parse_time(time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime()))
        pacing.append_log(log, 'claude', 97, now - timedelta(minutes=5))
        cls.cfg = cfg_of({'machines': {'here': {'host': 'local'}}, 'workers': {'cursor': {'kind': 'cli', 'command': ['x', '{prompt}']}},
                          'accounts': {'claude': {'window': '7d', 'resets': (now + timedelta(days=3)).isoformat()}},
                          'usage': {'log': log}}, folder)
        collector = dashboard.Collector(cls.cfg, probe_fn=lambda m: routing.Probe(True, 1, 0.5, 4, False),
                                        available_fn=lambda w: (True, 'ok'), stored_fn=lambda m: None)
        collector.refresh()
        cls.httpd = dashboard.server(cls.cfg, 0, collector)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def get(self, path, host=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        c.request('GET', path, headers={'Host': host or f'127.0.0.1:{self.port}', **(headers or {})})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def test_binds_to_loopback_only(self):
        self.assertEqual(self.httpd.server_address[0], '127.0.0.1')

    def test_api_state_carries_the_real_data(self):
        r, body = self.get('/api/state')
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader('Cache-Control'), 'no-cache')   # kept by the browser, always asked again with its ETag
        s = json.loads(body)
        self.assertEqual(s['summary']['panic'], ['claude'])
        self.assertEqual(s['machines']['items'][0]['status'], 'free')
        self.assertTrue(s['workers']['items'][0]['ready'])
        self.assertIn('jobs', s)

    def test_api_state_answers_304_when_unchanged_and_gzips_when_asked(self):
        with mock.patch.object(dashboard, 'STATE_FRESH', 600):   # a slow test machine must not build a new answer in between
            self.check_304_and_gzip()

    def check_304_and_gzip(self):
        r, body = self.get('/api/state')
        etag = r.getheader('ETag')
        self.assertTrue(etag.startswith('W/"'))
        r2, body2 = self.get('/api/state', headers={'If-None-Match': etag})
        self.assertEqual((r2.status, body2), (304, b''))   # within STATE_FRESH: the same answer
        self.assertIsNone(r2.getheader('Content-Length'))   # a 304 never states the empty body's length
        r3, body3 = self.get('/api/state', headers={'If-None-Match': 'W/"other"', 'Accept-Encoding': 'gzip, deflate'})
        self.assertEqual((r3.status, r3.getheader('Content-Encoding')), (200, 'gzip'))
        self.assertEqual(gzip.decompress(body3), body)
        self.assertLess(len(body3), len(body))
        stats = self.httpd.collector.state_cache.stats
        self.assertGreaterEqual(stats['not_modified'], 1)

    def test_the_state_cache_builds_again_after_it_goes_stale(self):
        t = [0.0]
        c = dashboard.StateCache(clock=lambda: t[0])
        snaps = iter([{'n': 1}, {'n': 2}])
        fake = SimpleNamespace(snapshot=lambda: next(snaps))
        first = c.get(fake)
        self.assertEqual(c.get(fake), first)
        t[0] += dashboard.STATE_FRESH
        self.assertNotEqual(c.get(fake)[1], first[1])
        self.assertEqual(c.stats['built'], 2)
        code, body, extra = dashboard.state_response(SimpleNamespace(state_cache=c), {})
        self.assertEqual((code, 'Content-Encoding' in extra), (200, False))   # small, and no Accept-Encoding: as it is

    def test_the_app_shell_and_pwa_files(self):
        r, body = self.get('/')
        self.assertEqual((r.status, r.getheader('Content-Type')), (200, 'text/html; charset=utf-8'))
        self.assertIn(b'manifest.webmanifest', body)
        self.assertIn("default-src 'self'", r.getheader('Content-Security-Policy'))
        r, body = self.get('/manifest.webmanifest')
        self.assertEqual(r.getheader('Content-Type'), 'application/manifest+json')
        icons = json.loads(body)['icons']
        self.assertTrue(any(i['purpose'] == 'maskable' for i in icons))
        for i in icons:
            self.assertEqual(self.get('/' + i['src'])[0].status, 200)
        self.assertEqual(self.get('/sw.js')[0].status, 200)

    def test_other_hosts_and_paths_outside_the_app_are_refused(self):
        self.assertEqual(self.get('/api/state', host='attacker.example')[0].status, 403)
        self.assertEqual(self.get('/', host=f'localhost:{self.port}')[0].status, 200)
        self.assertEqual(self.get('/../tutti/cli.py')[0].status, 404)
        self.assertEqual(self.get('/nope.js')[0].status, 404)
        self.assertEqual(self.get('/api/health')[0].status, 200)


class CliTest(unittest.TestCase):
    def test_once_prints_the_state_without_probing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'tutti.config.json')
            with open(path, 'w', encoding='utf8') as f:
                json.dump({'project': {'state': os.path.join(folder, 'state')}, 'accounts': {'agy': {}}}, f)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(['--config', path, 'dashboard', '--once', '--no-probe']), 0)
            s = json.loads(out.getvalue())
            self.assertEqual(s['usage']['accounts'][0]['state'], 'no reset')
            self.assertEqual(s['machines']['items'], [])

    def test_a_busy_port_names_the_fix(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = cfg_of({}, folder)
            httpd = dashboard.server(cfg, 0, dashboard.Collector(cfg))
            try:
                path = os.path.join(folder, 'tutti.config.json')
                with open(path, 'w', encoding='utf8') as f:
                    json.dump({'project': {'state': os.path.join(folder, 'state')}}, f)
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = cli.main(['--config', path, 'dashboard', '--port', str(httpd.server_address[1]), '--no-probe'])
                self.assertEqual(rc, 1)
                self.assertIn('--port', err.getvalue())
            finally:
                httpd.server_close()


if __name__ == '__main__':
    unittest.main()
