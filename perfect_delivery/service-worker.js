// Perfect Delivery — Service Worker
// Handles offline form caching and submission queuing

const SW_VERSION = 'pd-v1';
const CACHE_NAME = SW_VERSION + '-cache';

// Assets to cache on install (app shell)
const STATIC_ASSETS = [
  '/pd/offline',
];

// ── Install ────────────────────────────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

// ── Activate ───────────────────────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// ── Fetch ──────────────────────────────────────────────────────────────────
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Only handle same-origin /pd/ requests
  if (url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith('/pd/')) return;

  // Form page — cache-first with network fallback
  if (event.request.method === 'GET' && isFormPage(url.pathname)) {
    event.respondWith(cacheFirstWithNetworkFallback(event.request));
    return;
  }

  // Submit / draft endpoints — queue if offline
  if (event.request.method === 'POST') {
    event.respondWith(postWithOfflineQueue(event.request));
    return;
  }

  // Everything else — network first
  event.respondWith(networkFirstWithCacheFallback(event.request));
});

function isFormPage(pathname) {
  // Match /pd/<token> — form pages
  const parts = pathname.split('/').filter(Boolean);
  return parts.length === 2 && parts[0] === 'pd' && !parts[1].includes('.');
}

async function cacheFirstWithNetworkFallback(request) {
  const cached = await caches.match(request);
  if (cached) {
    // Refresh cache in background
    fetch(request).then(resp => {
      if (resp.ok) {
        caches.open(CACHE_NAME).then(c => c.put(request, resp.clone()));
      }
    }).catch(() => {});
    return cached;
  }
  try {
    const resp = await fetch(request);
    if (resp.ok) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, resp.clone());
    }
    return resp;
  } catch(e) {
    const offlineResp = await caches.match('/pd/offline');
    return offlineResp || new Response('Offline', { status: 503 });
  }
}

async function networkFirstWithCacheFallback(request) {
  try {
    const resp = await fetch(request);
    if (resp.ok) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, resp.clone());
    }
    return resp;
  } catch(e) {
    const cached = await caches.match(request);
    return cached || new Response('Offline', { status: 503 });
  }
}

async function postWithOfflineQueue(request) {
  try {
    const resp = await fetch(request.clone());
    return resp;
  } catch(e) {
    // Offline — queue the submission
    if (request.url.includes('/submit') || request.url.includes('/draft')) {
      await queueRequest(request);
      // Return a fake success so the form doesn't show an error
      return new Response(JSON.stringify({
        ok: true,
        queued: true,
        message: 'Saved offline — will submit when signal returns',
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    throw e;
  }
}

// ── Offline queue using IndexedDB ─────────────────────────────────────────
async function getDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('pd-offline-queue', 1);
    req.onupgradeneeded = e => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains('queue')) {
        db.createObjectStore('queue', { keyPath: 'id', autoIncrement: true });
      }
    };
    req.onsuccess = e => resolve(e.target.result);
    req.onerror   = e => reject(e.target.error);
  });
}

async function queueRequest(request) {
  try {
    const db = await getDB();
    // Read the request body
    const clone = request.clone();
    const body  = await clone.arrayBuffer();
    const headers = {};
    request.headers.forEach((v, k) => { headers[k] = v; });

    const entry = {
      url:       request.url,
      method:    request.method,
      headers:   headers,
      body:      body,
      timestamp: Date.now(),
    };

    const tx    = db.transaction('queue', 'readwrite');
    const store = tx.objectStore('queue');
    await new Promise((res, rej) => {
      const r = store.add(entry);
      r.onsuccess = res;
      r.onerror   = rej;
    });

    // Notify clients
    const clients = await self.clients.matchAll();
    clients.forEach(c => c.postMessage({ type: 'QUEUED', url: request.url }));

    console.log('[SW] Queued offline:', request.url);
  } catch(e) {
    console.error('[SW] Queue error:', e);
  }
}

async function flushQueue() {
  try {
    const db    = await getDB();
    const tx    = db.transaction('queue', 'readonly');
    const store = tx.objectStore('queue');
    const items = await new Promise((res, rej) => {
      const r = store.getAll();
      r.onsuccess = e => res(e.target.result);
      r.onerror   = rej;
    });

    if (!items.length) return;

    console.log('[SW] Flushing', items.length, 'queued requests');

    for (const item of items) {
      try {
        const resp = await fetch(item.url, {
          method:  item.method,
          headers: item.headers,
          body:    item.body,
        });

        if (resp.ok) {
          // Remove from queue
          const delTx    = db.transaction('queue', 'readwrite');
          const delStore = delTx.objectStore('queue');
          await new Promise((res, rej) => {
            const r = delStore.delete(item.id);
            r.onsuccess = res;
            r.onerror   = rej;
          });

          // Notify clients
          const clients = await self.clients.matchAll();
          clients.forEach(c => c.postMessage({
            type:    'SYNCED',
            url:     item.url,
            queued:  items.length,
          }));

          console.log('[SW] Synced:', item.url);
        }
      } catch(e) {
        console.log('[SW] Still offline, keeping in queue:', item.url);
        break; // Stop trying if still offline
      }
    }
  } catch(e) {
    console.error('[SW] Flush error:', e);
  }
}

// ── Background sync ────────────────────────────────────────────────────────
self.addEventListener('sync', event => {
  if (event.tag === 'pd-sync') {
    event.waitUntil(flushQueue());
  }
});

// ── Online message from client ────────────────────────────────────────────
self.addEventListener('message', event => {
  if (event.data && event.data.type === 'ONLINE') {
    flushQueue();
  }
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});
