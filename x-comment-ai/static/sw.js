/* RIFAT < AI — service worker
 * Strategy: app-shell precache + network-first for HTML, cache-first for static.
 * /generate, /regenerate are NEVER cached (live data).
 */
const VERSION = 'v10-theme';
const SHELL_CACHE = `rifatai-shell-${VERSION}`;
const RUNTIME_CACHE = `rifatai-runtime-${VERSION}`;

const SHELL_ASSETS = [
  '/',
  '/static/style.css',
  '/static/app.js',
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
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== SHELL_CACHE && k !== RUNTIME_CACHE)
          .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

const isAPI = (url) =>
  url.pathname === '/generate' ||
  url.pathname === '/generate/stream' ||
  url.pathname === '/regenerate' ||
  url.pathname === '/preview' ||
  url.pathname === '/ocr' ||
  url.pathname === '/chat/stream' ||
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

  // Network-first for the HTML document.
  if (req.mode === 'navigate' || req.destination === 'document') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(RUNTIME_CACHE).then((c) => c.put(req, copy)).catch(() => {});
          return res;
        })
        .catch(async () => {
          const cached = await caches.match(req);
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
