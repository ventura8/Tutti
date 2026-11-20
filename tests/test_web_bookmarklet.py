
import json
import os
import shutil
import subprocess
import unittest

@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class TestBookmarkletUnit(unittest.TestCase):
    def setUp(self):
        with open('/home/sergiu-alexandrescu/Projects/hcm-wt/tutti-run-ap-t111/tutti/web/helper-bookmarklet.js', 'r') as f:
            self.js_template = f.read()

    def test_good_case(self):
        js = self.js_template.replace('__TARGETS__', json.dumps([{'account': 'test-account', 'label': 'Test Account'}]))
        js = js.replace('__KEY__', '"test-key"')
        js = js.replace('__ORIGIN__', '"http://test-origin"')
        js = js.replace('__PARSE__', '(text, targets) => ({loading: false, readings: [{account: "test-account", used: 10, resets: "2026-01-01", how: "test"}], missing: []})')
        
        node_cmd = f"""
            let opened = null;
            const alerts = [];
            const alert = (msg) => {{ alerts.push(msg); }};
            const location = {{host: "test.com", pathname: "/test"}};
            const document = {{body: {{innerText: "some text"}}}};
            const window = {{open: (u) => {{ opened = u; }}}};
            
            // Execute the bookmarklet
            {js}
            
            console.log(JSON.stringify({{opened: opened, alerts: alerts}}));
        """
        
        out = subprocess.run(['node', '-e', node_cmd], capture_output=True, text=True, check=True)
        payload = json.loads(out.stdout)
        
        self.assertEqual(payload['alerts'], [])
        self.assertIsNotNone(payload['opened'])
        self.assertTrue(payload['opened'].startswith('http://test-origin/#reading='))

    def test_bad_case(self):
        js = self.js_template.replace('__TARGETS__', json.dumps([{'account': 'test-account', 'label': 'Test Account'}]))
        js = js.replace('__KEY__', '"test-key"')
        js = js.replace('__ORIGIN__', '"http://test-origin"')
        js = js.replace('__PARSE__', '(text, targets) => ({loading: true, readings: [], missing: []})')
        
        node_cmd = f"""
            let opened = null;
            const alerts = [];
            const alert = (msg) => {{ alerts.push(msg); }};
            const location = {{host: "test.com", pathname: "/test"}};
            const document = {{body: {{innerText: "loading"}}}};
            const window = {{open: (u) => {{ opened = u; }}}};
            
            // Execute the bookmarklet
            {js}
            
            console.log(JSON.stringify({{opened: opened, alerts: alerts}}));
        """
        
        out = subprocess.run(['node', '-e', node_cmd], capture_output=True, text=True, check=True)
        payload = json.loads(out.stdout)
        
        self.assertEqual(len(payload['alerts']), 1)
        self.assertIn('still loading', payload['alerts'][0])
        self.assertIsNone(payload['opened'])
