"""The dashboard service worker (tutti/web/sw.js): precaches the shell, drops old caches, ignores /api/, and falls back safely."""
import json
import os
import shutil
import subprocess
import unittest

from tutti import __version__, dashboard


def sw_case(assertion_js):
    with open(os.path.join(dashboard.WEB, 'sw.js'), encoding='utf8') as f:
        src = f.read().replace('__TUTTI_VERSION__', __version__)
    script = f"""
(async () => {{
    const handlers = Object.create(null);
    const opened = [];
    const added = [];
    const deleted = [];
    const storage = new Map();

    globalThis.self = {{
        location: {{ href: 'https://dashboard.example/sw.js', origin: 'https://dashboard.example' }},
        _skip: 0,
        _claimed: false,
        addEventListener: (name, fn) => {{ handlers[name] = fn; }},
        skipWaiting: () => {{ self._skip += 1; return Promise.resolve(); }},
        clients: {{ claim: () => {{ self._claimed = true; return Promise.resolve(); }} }},
    }};

    const mockCache = {{
        addAll: async (items) => {{ added.push(...items); }},
        match: async (req, opts = {{}}) => {{
            const key = typeof req === 'string' ? req : req.url;
            if (opts.ignoreSearch && typeof req !== 'string') {{
                const stripped = new URL(key).origin + new URL(key).pathname;
                return storage.get(stripped) || storage.get(key) || null;
            }}
            return storage.get(key) || null;
        }},
        put: async (req, res) => {{
            const key = typeof req === 'string' ? req : req.url;
            storage.set(key, res);
        }},
    }};

    globalThis.caches = {{
        open: async (name) => {{
            opened.push(name);
            return mockCache;
        }},
        keys: async () => ['tutti-shell-old-0.1.0', 'tutti-shell-{__version__}'],
        delete: async (k) => {{ deleted.push(k); return true; }},
        match: async (req) => {{
            const key = typeof req === 'string' ? req : req.url;
            return storage.get(key) || null;
        }},
    }};

    globalThis.fetch = async () => new Response('network-ok', {{ status: 200 }});

    {src}

    {assertion_js}
}})().catch((err) => {{
    console.error(err && err.stack ? err.stack : String(err));
    process.exit(1);
}});
"""
    proc = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True, timeout=10)
    return json.loads(proc.stdout)


@unittest.skipUnless(shutil.which('node'), 'node is not installed: sw.js behavior checks run where node is available (CI has it)')
class ServiceWorker(unittest.TestCase):
    def test_install_precaches_the_shell_and_skips_waiting(self):
        got = sw_case("""
    const event = { waitUntil(p) { this.done = p; } };
    handlers.install(event);
    await event.done;
    console.log(JSON.stringify({
        opened,
        addedCount: added.length,
        hasApp: added.includes('app.js' + V),
        skip: self._skip,
    }));
""")
        self.assertEqual(got['opened'], [f'tutti-shell-{__version__}'])
        self.assertGreater(got['addedCount'], 10)
        self.assertTrue(got['hasApp'])
        self.assertEqual(got['skip'], 1)

    def test_activate_drops_old_caches_and_claims_clients(self):
        got = sw_case("""
    const event = { waitUntil(p) { this.done = p; } };
    handlers.activate(event);
    await event.done;
    console.log(JSON.stringify({ deleted, claimed: self._claimed }));
""")
        self.assertEqual(got['deleted'], ['tutti-shell-old-0.1.0'])
        self.assertTrue(got['claimed'])

    def test_message_from_same_origin_skips_waiting_but_foreign_origin_is_ignored(self):
        got = sw_case("""
    handlers.message({ origin: 'https://attacker.example', data: { type: 'skip-waiting' } });
    const afterForeign = self._skip;
    handlers.message({ origin: 'https://dashboard.example', data: { type: 'skip-waiting' } });
    const afterOwn = self._skip;
    console.log(JSON.stringify({ afterForeign, afterOwn }));
""")
        self.assertEqual(got['afterForeign'], 0)
        self.assertEqual(got['afterOwn'], 1)

    def test_fetch_ignores_api_and_non_get_and_cross_origin_requests(self):
        got = sw_case("""
    let handledApi = false;
    handlers.fetch({
        request: new Request('https://dashboard.example/api/state'),
        respondWith() { handledApi = true; },
    });

    let handledPost = false;
    handlers.fetch({
        request: new Request('https://dashboard.example/app.js', { method: 'POST' }),
        respondWith() { handledPost = true; },
    });

    let handledCross = false;
    handlers.fetch({
        request: new Request('https://attacker.example/app.js'),
        respondWith() { handledCross = true; },
    });

    console.log(JSON.stringify({ handledApi, handledPost, handledCross }));
""")
        self.assertFalse(got['handledApi'])
        self.assertFalse(got['handledPost'])
        self.assertFalse(got['handledCross'])

    def test_fetch_serves_cached_asset(self):
        got = sw_case("""
    storage.set('https://dashboard.example/app.js' + V, new Response('cached-app'));
    let responseText = null;
    const event = {
        request: new Request('https://dashboard.example/app.js' + V),
        respondWith(promise) { this.response = promise; },
        waitUntil() {},
    };
    handlers.fetch(event);
    const res = await event.response;
    responseText = await res.text();
    console.log(JSON.stringify({ responseText }));
""")
        self.assertEqual(got['responseText'], 'cached-app')

    def test_fetch_ignores_search_for_unversioned_assets(self):
        got = sw_case("""
    storage.set('https://dashboard.example/app.js', new Response('cached-base'));
    globalThis.fetch = async () => { throw new Error('network down'); };
    const event = {
        request: new Request('https://dashboard.example/app.js?theme=dark'),
        respondWith(promise) { this.response = promise; },
        waitUntil() {},
    };
    handlers.fetch(event);
    const res = await event.response;
    console.log(JSON.stringify({ responseText: await res.text() }));
""")
        self.assertEqual(got['responseText'], 'cached-base')

    def test_fetch_versioned_asset_does_not_use_other_version_cache(self):
        got = sw_case("""
    storage.set('https://dashboard.example/app.js?v=old-release', new Response('stale-version'));
    globalThis.fetch = async () => { throw new Error('network down'); };
    const event = {
        request: new Request('https://dashboard.example/app.js' + V),
        respondWith(promise) { this.response = promise; },
        waitUntil() {},
    };
    handlers.fetch(event);
    const res = await event.response;
    const text = res.type === 'error' ? null : await res.text();
    console.log(JSON.stringify({ isError: res.type === 'error', text }));
""")
        self.assertTrue(got['isError'])
        self.assertIsNone(got['text'])

    def test_fetch_offline_fallback_prevents_hanging(self):
        got = sw_case("""
    globalThis.fetch = async () => { throw new Error('network down'); };
    const event = {
        request: new Request('https://dashboard.example/missing.js'),
        respondWith(promise) { this.response = promise; },
        waitUntil() {},
    };
    handlers.fetch(event);
    const res = await event.response;
    console.log(JSON.stringify({ isError: res && res.type === 'error' }));
""")
        self.assertTrue(got['isError'])

    def test_fetch_returns_an_error_when_cache_open_fails(self):
        got = sw_case("""
    caches.open = async () => { throw new Error('cache unavailable'); };
    const event = {
        request: new Request('https://dashboard.example/app.js'),
        respondWith(promise) { this.response = promise; },
        waitUntil() {},
    };
    handlers.fetch(event);
    const res = await event.response;
    console.log(JSON.stringify({ isError: res.type === 'error' }));
""")
        self.assertTrue(got['isError'])


if __name__ == '__main__':
    unittest.main()
