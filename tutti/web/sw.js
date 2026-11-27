// Tutti dashboard service worker: the app shell works offline (stale-while-revalidate); /api/ is never cached, so the
// page always shows live data or says plainly that the local service is not answering. The page itself is network-first:
// it carries the running server's token for saving readings, so a cached copy is only the offline fallback.
// The server writes its version here: a new release is a new worker, its own cache, and the old cache is dropped.
const CACHE = 'tutti-shell-__TUTTI_VERSION__';
const V = '?v=__TUTTI_VERSION__';   // the page loads its code with the release in the URL (index.html)
// the page itself is not precached: with sign-in on it carries the session's CSRF token (it is kept only when not no-store)
const SHELL = [`app.css${V}`, `format.js${V}`, `a11y.js${V}`, `telemetry.js${V}`, `update.js${V}`, `attention.js${V}`, `app.js${V}`, `devices.js${V}`, `controls.js${V}`, `connect.js${V}`, `settings.js${V}`, `setup.js${V}`, `household.js${V}`, `energy.js${V}`, `queue.js${V}`, `where.js${V}`, `outside.js${V}`, `vpn.js${V}`, `qr.js${V}`, `users.js${V}`, `welcome.js${V}`, `needs.js${V}`, `github.js${V}`,
    `agents.js${V}`,
    `chat.js${V}`, `chatctl.js${V}`, `userstab.js${V}`, `report.js${V}`, 'manifest.webmanifest', 'icons/icon.svg', 'icons/icon-192.png',
    'icons/icon-512.png', 'icons/maskable-512.png', 'icons/apple-touch-icon.png'];

self.addEventListener('install', (event) => {
    event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

// The page asks a waiting worker to take over at once when it sees a new version in /api/state.
self.addEventListener('message', (event) => {
    if (new URL(event.origin || 'null', self.location.href).origin !== self.location.origin) return;   // our own pages only
    if (event.data?.type === 'skip-waiting') self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(caches.keys()
        .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
        .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);
    if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) {
        return;
    }
    if (event.request.mode === 'navigate') {
        event.respondWith(fetch(event.request).then(async (res) => {
            // a signed-in page (no-store: it carries the session's CSRF token) or the sign-in page is never kept
            if (res.ok && !(res.headers.get('Cache-Control') || '').includes('no-store')) await (await caches.open(CACHE)).put('index.html', res.clone());
            return res;
        }).catch(async () => (await caches.match('index.html')) || Response.error()));
        return;
    }
    event.respondWith(caches.open(CACHE).then(async (cache) => {
        // a versioned asset matches only its own release: an old worker fetches a new page's code from the network
        const cached = await cache.match(event.request, { ignoreSearch: !url.searchParams.has('v') });
        const fresh = fetch(event.request).then(async (res) => {
            if (res.ok && !(res.headers.get('Cache-Control') || '').includes('no-store')) {
                await cache.put(event.request, res.clone());
            }
            return res;
        }).catch(() => cached);
        event.waitUntil(fresh.then(() => undefined, () => undefined));   // keep the worker alive until the cache write ends
        return cached || fresh;
    }));
});
