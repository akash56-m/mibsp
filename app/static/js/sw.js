const CACHE_NAME = 'mibsp-v5';
const CORE_ASSETS = [
  '/',
  '/static/manifest.json',
  '/static/css/style.css',
  '/static/js/main.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon-maskable-512.png',
  '/static/icons/apple-touch-icon-180.png'
];

function isApiRequest(request) {
  const url = new URL(request.url);
  return url.pathname.startsWith('/api/');
}

function isNavigationRequest(request) {
  return request.mode === 'navigate';
}

function isCacheableAsset(request) {
  return request.destination === 'style'
    || request.destination === 'script'
    || request.destination === 'image'
    || request.destination === 'font'
    || request.destination === 'manifest'
    || request.url.endsWith('.css')
    || request.url.endsWith('.js')
    || request.url.endsWith('.png')
    || request.url.endsWith('.jpg')
    || request.url.endsWith('.jpeg')
    || request.url.endsWith('.webp')
    || request.url.endsWith('.svg')
    || request.url.endsWith('.ico');
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') {
    return;
  }

  const request = event.request;
  const url = new URL(request.url);

  if (isApiRequest(request)) {
    event.respondWith((async function () {
      try {
        return await fetch(new Request(request, { cache: 'no-store' }));
      } catch (_) {
        if (
          request.headers.get('Accept')
          && request.headers.get('Accept').includes('application/json')
        ) {
          return new Response(
            JSON.stringify({ error: 'Network is unavailable. Please try again.' }),
            {
              status: 503,
              headers: {
                'Content-Type': 'application/json',
              },
            }
          );
        }
        return new Response('Network unavailable.', { status: 503 });
      }
    })());
    return;
  }

  // Keep HTML pages fresh after deployment while still allowing offline fallback.
  if (isNavigationRequest(request)) {
    event.respondWith((async function () {
      try {
        const response = await fetch(request);
        if (response && response.status === 200 && url.origin === self.location.origin) {
          const cache = await caches.open(CACHE_NAME);
          cache.put(request, response.clone());
        }
        return response;
      } catch (_) {
        const cached = await caches.match(request);
        if (cached) {
          return cached;
        }
        const fallback = await caches.match('/');
        return fallback || new Response('Offline', { status: 503 });
      }
    })());
    return;
  }

  // Static assets: cache-first.
  event.respondWith((async function () {
    const cached = await caches.match(request);
    if (cached) {
      return cached;
    }

    try {
      const response = await fetch(request);
      if (
        response
        && response.status === 200
        && isCacheableAsset(request)
        && url.origin === self.location.origin
      ) {
        const cache = await caches.open(CACHE_NAME);
        cache.put(request, response.clone());
      }
      return response;
    } catch (_) {
      const fallback = await caches.match('/');
      return fallback || new Response('Offline', { status: 503 });
    }
  })());
});
