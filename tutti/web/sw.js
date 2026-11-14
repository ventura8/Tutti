// Tutti dashboard service worker: the app shell works offline (stale-while-revalidate); /api/ is never cached, so the
// page always shows live data or says plainly that the local service is not answering.
const CACHE = 'tutti-shell-v1';
const SHELL = ['./', 'index.html', 'app.css', 'app.js', 'manifest.webmanifest', 'icons/icon.svg', 'icons/icon-192.png',
    'icons/icon-512.png', 'icons/maskable-512.png', 'icons/apple-touch-icon.png'];

self.addEventListener('install', (event) => {
    event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
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
    event.respondWith(caches.open(CACHE).then(async (cache) => {
        const cached = await cache.match(event.request, { ignoreSearch: true });
        const fresh = fetch(event.request).then((res) => {
            if (res.ok) {
                cache.put(event.request, res.clone());
            }
            return res;
        }).catch(() => cached);
        return cached || fresh;
    }));
});
