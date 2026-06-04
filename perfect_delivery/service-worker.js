// Perfect Delivery Service Worker v2
// Strategy: Cache form pages aggressively, queue submissions when offline

const CACHE = 'pd-v2';

// ── Install ────────────────────────────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(['/pd/offline']))
      .then(() => self.skipWaiting())
  );
});

// ── Activate ──────────────────────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// ── Fetch ─────────────────────────────────────────────────────────────────
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // Only handle /pd/ same-origin requests
  if (url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith('/pd/')) return;

  // POST requests (submit, draft) — try network, queue if offline
  if (req.method === 'POST') {
    event.respondWith(handlePost(req));
    return;
  }

  // GET — cache then network (stale-while-revalidate)
  if (req.method === 'GET') {
    event.respondWith(staleWhileRevalidate(req));
    return;
  }
});

// Cache then revalidate in background
async function staleWhileRevalidate(req) {
  const cache  = await caches.open(CACHE);
  const cached = await cache.match(req);

  const fetchPromise = fetch(req).then(resp => {
    if (resp.ok && resp.status === 200) {
      cache.put(req, resp.clone());
    }
    return resp;
  }).catch(() => null);

  return cached || fetchPromise || caches.match('/pd/offline');
}

// Try network, queue if offline
async function handlePost(req) {
  try {
    const resp = await fetch(req.clone(), { signal: AbortSignal.timeout(10000) });
    return resp;
  } catch(e) {
    // Offline — queue it
    const isSubmit = req.url.includes('/submit');
    const isDraft  = req.url.includes('/draft');

    if (isSubmit || isDraft) {
      await enqueue(req);
      return new Response(JSON.stringify({
        ok:      true,
        queued:  true,
        offline: true,
        message: 'Saved offline',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    throw e;
  }
}

// ── IndexedDB queue ────────────────────────────────────────────────────────
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
  const db   = await openDB();
  const body = await req.clone().arrayBuffer();
  const hdrs = {};
  req.headers.forEach((v, k) => { hdrs[k] = v; });

  return new Promise((res, rej) => {
    const tx = db.transaction('requests', 'readwrite');
    tx.objectStore('requests').add({
      url:       req.url,
      method:    req.method,
      headers:   hdrs,
      body:      body,
      ts:        Date.now(),
    });
    tx.oncomplete = res;
    tx.onerror    = rej;
  });
}

async function flushQueue() {
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
        method:  item.method,
        headers: item.headers,
        body:    item.body,
      });
      if (resp.ok || resp.status === 200) {
        await new Promise((res, rej) => {
          const tx = db.transaction('requests', 'readwrite');
          tx.objectStore('requests').delete(item.id);
          tx.oncomplete = res;
          tx.onerror    = rej;
        });
        synced++;
      }
    } catch(e) {
      break; // Still offline
    }
  }

  if (synced) {
    const clients = await self.clients.matchAll({ includeUncontrolled: true });
    clients.forEach(c => c.postMessage({ type: 'SYNCED', count: synced }));
  }
}

// Background sync
self.addEventListener('sync', event => {
  if (event.tag === 'pd-sync') event.waitUntil(flushQueue());
});

// Manual trigger from page
self.addEventListener('message', event => {
  if (event.data?.type === 'FLUSH') flushQueue();
  if (event.data?.type === 'SKIP_WAITING') self.skipWaiting();
});
