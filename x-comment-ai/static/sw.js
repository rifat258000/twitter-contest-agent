/* RIFAT < AI — service worker
 * Strategy: app-shell precache + network-first for HTML, cache-first for static.
 * /generate, /regenerate are NEVER cached (live data).
 */
const VERSION = 'v15-no-chat';
const SHELL_CACHE = `rifatai-shell-${VERSION}`;
const RUNTIME_CACHE = `rifatai-runtime-${VERSION}`;

const SHELL_ASSETS = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/static/vendor/jspdf.umd.min.js',
  '/static/manifest.webmanifest',
  '/static/icons/icon-180.png',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
  '/static/offline.html',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      // Clear every old cache (including stale runtime entries from prior
      // versions), and the current runtime cache too — a poisoned navigate
      // entry there would otherwise survive a SW upgrade and keep serving
      // the offline page as if it were the homepage.
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k))
      );
      await self.clients.claim();
    })()
  );
});

const isAPI = (url) =>
  url.pathname === '/generate' ||
  url.pathname === '/generate/stream' ||
  url.pathname === '/regenerate' ||
  url.pathname === '/preview' ||
  url.pathname === '/ocr' ||
  url.pathname === '/health' ||
  url.pathname.startsWith('/contests/');

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // Same-origin only.
  if (url.origin !== self.location.origin) return;

  // Never cache live API endpoints.
  if (isAPI(url)) return;

  // Network-first for the HTML document. We *only* cache an OK (2xx) response
  // and *only* fall back to the offline shell if the cache also has nothing —
  // this prevents the runtime cache from being poisoned by a 5xx during a
  // deploy window, which would otherwise pin the offline page as the homepage.
  if (req.mode === 'navigate' || req.destination === 'document') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(RUNTIME_CACHE).then((c) => c.put(req, copy)).catch(() => {});
          }
          return res;
        })
        .catch(async () => {
          const cached = (await caches.match(req)) || (await caches.match('/'));
          if (cached) return cached;
          return caches.match('/static/offline.html');
        })
    );
    return;
  }

  // Cache-first for static assets.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req)
          .then((res) => {
            const copy = res.clone();
            caches.open(RUNTIME_CACHE).then((c) => c.put(req, copy)).catch(() => {});
            return res;
          })
          // On cache miss + network failure, respondWith() must still receive
          // a Response object — returning `cached` here would be `undefined`
          // and crash the worker. Synthesize a 408 instead.
          .catch(() => cached || new Response('', { status: 408, statusText: 'Offline (asset unavailable)' }));
      })
    );
  }
});
