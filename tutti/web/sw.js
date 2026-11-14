// Tutti dashboard service worker: the app shell works offline (stale-while-revalidate); /api/ is never cached, so the
// page always shows live data or says plainly that the local service is not answering. The page itself is network-first:
// it carries the running server's token for saving readings, so a cached copy is only the offline fallback.
// The server writes its version here: a new release is a new worker, its own cache, and the old cache is dropped.
const CACHE = 'tutti-shell-__TUTTI_VERSION__';
const SHELL = ['./', 'index.html', 'app.css', 'update.js', 'app.js', 'manifest.webmanifest', 'icons/icon.svg', 'icons/icon-192.png',
    'icons/icon-512.png', 'icons/maskable-512.png', 'icons/apple-touch-icon.png'];

self.addEventListener('install', (event) => {
    event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

// The page asks a waiting worker to take over at once when it sees a new version in /api/state.
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'skip-waiting') self.skipWaiting();
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
            if (res.ok) await (await caches.open(CACHE)).put('index.html', res.clone());
            return res;
        }).catch(async () => (await caches.match('index.html')) || Response.error()));
        return;
    }
    event.respondWith(caches.open(CACHE).then(async (cache) => {
        const cached = await cache.match(event.request, { ignoreSearch: true });
        const fresh = fetch(event.request).then(async (res) => {
            if (res.ok) {
                await cache.put(event.request, res.clone());
            }
            return res;
        }).catch(() => cached);
        event.waitUntil(fresh.then(() => undefined, () => undefined));   // keep the worker alive until the cache write ends
        return cached || fresh;
    }));
});
