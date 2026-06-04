// Perfect Delivery Service Worker v5
// Only handles page caching — submissions queued by form itself
const CACHE = 'pd-v5';
const OFFLINE_URL = '/pd/offline';

self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(CACHE)
      .then(function(cache) { return cache.add(OFFLINE_URL); })
      .then(function() { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys().then(function(keys) {
      return Promise.all(
        keys.filter(function(k) { return k !== CACHE; })
            .map(function(k) { return caches.delete(k); })
      );
    }).then(function() { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function(event) {
  var req = event.request;
  var url = new URL(req.url);

  if (url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith('/pd/')) return;
  if (req.method !== 'GET') return; // Don't intercept POST — form handles offline queue itself

  event.respondWith(handleGet(req));
});

function handleGet(req) {
  var cache;
  return caches.open(CACHE).then(function(c) {
    cache = c;
    return c.match(req);
  }).then(function(cached) {
    // Try network, cache result, fall back to cached
    var networkResp = fetch(req).then(function(resp) {
      if (resp && resp.ok) {
        cache.put(req, resp.clone());
      }
      return resp;
    }).catch(function() { return null; });

    // Return cached immediately if available, else wait for network
    if (cached) {
      networkResp; // refresh in background
      return cached;
    }
    return networkResp.then(function(resp) {
      if (resp) return resp;
      return cache.match(OFFLINE_URL).then(function(offline) {
        return offline || new Response(
          '<html><body style="font-family:sans-serif;padding:40px;text-align:center;background:#1a1a2e;color:#fff"><h2 style="color:#C5962A">No Signal</h2><p>Open this page with signal first, then it will work offline.</p></body></html>',
          { status: 200, headers: { 'Content-Type': 'text/html' } }
        );
      });
    });
  });
}

self.addEventListener('message', function(event) {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});
