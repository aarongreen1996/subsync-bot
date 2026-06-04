// Perfect Delivery Service Worker v3
const CACHE = 'pd-v4';
const OFFLINE_URL = '/pd/offline';

// ── Install — cache offline page ──────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE)
      .then(cache => cache.add(OFFLINE_URL))
      .then(() => self.skipWaiting())
  );
});

// ── Activate — take control immediately ───────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// ── Fetch ─────────────────────────────────────────────────────────────────
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // Only handle same-origin /pd/ requests
  if (url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith('/pd/')) return;

  if (req.method === 'POST') {
    event.respondWith(handlePost(req));
    return;
  }

  if (req.method === 'GET') {
    event.respondWith(handleGet(req));
    return;
  }
});

// GET: serve from cache, fetch+update in background, fallback to offline page
async function handleGet(req) {
  const cache  = await caches.open(CACHE);
  const cached = await cache.match(req);

  // Always try to fetch fresh in background
  const networkFetch = fetch(req).then(resp => {
    if (resp && resp.ok && resp.status === 200) {
      // Only cache complete HTML pages and same-origin resources
      const ct = resp.headers.get('content-type') || '';
      if (ct.includes('text/html') || ct.includes('application/json')) {
        cache.put(req, resp.clone());
      }
    }
    return resp;
  }).catch(() => null);

  if (cached) {
    // Return cached immediately, refresh in background
    networkFetch; // fire and forget
    return cached;
  }

  // No cache — wait for network
  const networkResp = await networkFetch;
  if (networkResp) return networkResp;

  // Truly offline with nothing cached — return offline page
  const offlinePage = await cache.match(OFFLINE_URL);
  return offlinePage || new Response(
    '<html><body style="font-family:sans-serif;text-align:center;padding:40px"><h2>No Signal</h2><p>Open this page when you have signal first, then it will work offline.</p></body></html>',
    { status: 200, headers: { 'Content-Type': 'text/html' } }
  );
}

// POST: try network, queue if offline
async function handlePost(req) {
  try {
    const resp = await fetch(req.clone());
    return resp;
  } catch(e) {
    if (req.url.includes('/submit') || req.url.includes('/draft')) {
      await enqueue(req);
      return new Response(JSON.stringify({
        ok: true, queued: true, offline: true,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    throw e;
  }
}

// ── Queue ─────────────────────────────────────────────────────────────────
function openDB() {
  return new Promise((res, rej) => {
    const r = indexedDB.open('pd-queue', 1);
    r.onupgradeneeded = e => {
      e.target.result.createObjectStore('requests', { keyPath: 'id', autoIncrement: true });
    };
    r.onsuccess = e => res(e.target.result);
    r.onerror   = e => rej(e.target.error);
  });
}

async function enqueue(req) {
  try {
    const db   = await openDB();
    const body = await req.clone().arrayBuffer();
    const hdrs = {};
    req.headers.forEach((v, k) => { hdrs[k] = v; });
    const tx = db.transaction('requests', 'readwrite');
    tx.objectStore('requests').add({
      url: req.url, method: req.method,
      headers: hdrs, body, ts: Date.now(),
    });
    await new Promise((res, rej) => { tx.oncomplete = res; tx.onerror = rej; });
    const clients = await self.clients.matchAll({ includeUncontrolled: true });
    clients.forEach(c => c.postMessage({ type: 'QUEUED', url: req.url }));
  } catch(e) {
    console.error('[SW] enqueue error:', e);
  }
}

async function flushQueue() {
  try {
    const db = await openDB();
    const items = await new Promise((res, rej) => {
      const tx = db.transaction('requests', 'readonly');
      const r  = tx.objectStore('requests').getAll();
      r.onsuccess = e => res(e.target.result);
      r.onerror   = rej;
    });

    if (!items.length) return;
    let synced = 0;

    for (const item of items) {
      try {
        const resp = await fetch(item.url, {
          method: item.method, headers: item.headers, body: item.body,
        });
        if (resp.ok) {
          await new Promise((res, rej) => {
            const tx = db.transaction('requests', 'readwrite');
            tx.objectStore('requests').delete(item.id);
            tx.oncomplete = res; tx.onerror = rej;
          });
          synced++;
        }
      } catch(e) { break; }
    }

    if (synced > 0) {
      const clients = await self.clients.matchAll({ includeUncontrolled: true });
      clients.forEach(c => c.postMessage({ type: 'SYNCED', count: synced }));
    }
  } catch(e) {
    console.error('[SW] flushQueue error:', e);
  }
}

self.addEventListener('sync', event => {
  if (event.tag === 'pd-sync') event.waitUntil(flushQueue());
});

self.addEventListener('message', event => {
  if (event.data?.type === 'FLUSH') flushQueue();
  if (event.data?.type === 'SKIP_WAITING') self.skipWaiting();
});
