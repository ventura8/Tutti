"""The hosts API (t406): /api/hosts, /api/host-limit and /api/user/main-host reach the same handlers as the machine routes,
with "host" in the body; the old routes still answer, marked deprecated with a link to the new one. And the OpenAPI
(Swagger) document: made from docs/API.md, served, shown at /api-docs.html, the committed copy current."""
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

from tutti import apidoc, config, dashboard, hostapi, openapi, routing


class Pure(unittest.TestCase):
    def test_routes_bodies_and_deprecation(self):
        self.assertEqual(hostapi.route('/api/host-limit'), ('/api/machine-limit', True))
        self.assertEqual(hostapi.route('/api/state'), ('/api/state', False))
        self.assertEqual(json.loads(hostapi.body(b'{"host": "nuc", "window": "off"}')), {'host': 'nuc', 'window': 'off', 'machine': 'nuc'})
        self.assertEqual(hostapi.body(b'not json'), b'not json')
        self.assertEqual(hostapi.deprecation('/api/machine-limit'),
                         {'Deprecation': 'true', 'Link': '</api/host-limit>; rel="successor-version"'})
        self.assertEqual(hostapi.deprecation('/api/host-limit'), {})
        self.assertEqual(hostapi.hosts({'machines': {'items': [{'name': 'nuc'}]}}), {'hosts': [{'name': 'nuc'}]})
        self.assertEqual(hostapi.hosts({}), {'hosts': []})

    def test_the_openapi_document_comes_from_the_doc_and_is_current(self):
        doc = openapi.from_doc(apidoc.DOC)
        self.assertEqual(doc['openapi'], '3.1.0')
        self.assertEqual(doc['paths']['/api/host-limit']['post']['requestBody']['content']['application/json']['example']['host'],
                         'build1')
        self.assertTrue(doc['paths']['/api/machine-limit']['post']['deprecated'])
        self.assertIn('{id}', ''.join(doc['paths']))
        self.assertEqual(openapi.document('no section'), {**openapi.document('no section'), 'paths': {}})
        self.assertNotIn('docs/openapi.json is stale', ' '.join(apidoc.problems()))   # the committed copy is current

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_api_page_groups_routes_by_section(self):
        with open(os.path.join(dashboard.WEB, 'api-docs.js'), encoding='utf8') as f:
            src = f.read()
        doc = openapi.from_doc(apidoc.DOC)
        js = f'{src}\nconsole.log(JSON.stringify(apiSections({json.dumps(doc)}).map((s) => [s.tag, s.ops.length])));'
        sections = dict(json.loads(subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True).stdout))
        self.assertGreater(sections['Reading'], 5)
        self.assertIn('Changing (admin)', sections)


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(cls.tmp.name, 'tutti.config.yaml')
        with open(path, 'w', encoding='utf8') as f:
            f.write('machines:\n  nuc:\n    host: nuc.example\n')
        cls.cfg = config.normalise({'machines': {'nuc': {'host': 'nuc.example'}},
                                    'project': {'worktrees': cls.tmp.name, 'state': os.path.join(cls.tmp.name, 'state')}})
        cls.cfg['_path'] = path
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

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        origin = f'http://127.0.0.1:{self.port}'
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers={'Host': f'127.0.0.1:{self.port}', 'Origin': origin, 'Content-Type': 'application/json',
                           'X-Tutti-Token': self.httpd.token})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, json.loads(data or b'null')

    def test_the_new_routes_work_and_the_old_ones_are_marked_deprecated(self):
        r, d = self.call('GET', '/api/hosts')
        self.assertEqual((r.status, [h['name'] for h in d['hosts']]), (200, ['nuc']))
        r, d = self.call('POST', '/api/host-limit', {'host': 'nuc', 'window': '18-01', 'pct': 50})
        self.assertEqual(r.status, 200, d)
        self.assertIsNone(r.getheader('Deprecation'))
        r, d = self.call('POST', '/api/machine-limit', {'machine': 'nuc', 'window': 'off'})
        self.assertEqual((r.status, r.getheader('Deprecation')), (200, 'true'), d)
        self.assertIn('/api/host-limit', r.getheader('Link'))
        r, d = self.call('POST', '/api/host-limit', {'host': 'nope', 'window': '18-01'})
        self.assertGreaterEqual(r.status, 400)   # the same checks as before: an unknown host says so
        r, d = self.call('GET', '/api/openapi.json')
        self.assertEqual((r.status, d['openapi']), (200, '3.1.0'))


if __name__ == '__main__':
    unittest.main()
