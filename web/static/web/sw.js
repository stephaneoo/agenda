// The service worker, served at /sw.js (scope /) by web.views.service_worker, which fills in
// VERSION (a hash of the files it precaches) and PRECACHE.
//
// - Precaches the app shell ("/"), the CSS and JS, and the reference endpoints.
// - Static files: from the cache (it is replaced whenever VERSION changes).
// - Reference endpoints: the network first, the cache when offline.
// - Pages: the network first; any "/" visit refreshes the shell, visited occurrence pages
//   are kept, and dropped when the server answers 404 or 410.
//   Offline, a page comes from the cache, else the shell, which renders from IndexedDB.
// - /api/v1/occurrences is never cached here: app.js keeps its copy in IndexedDB.

const VERSION = "__VERSION__";
const PRECACHE = __PRECACHE__;
const SHELL = "/";
const STATIC = `agenda-static-${VERSION}`;
const PAGES = "agenda-pages";
const MAX_PAGES = 200;
const REFERENCE = new Set(["/api/v1/communes", "/api/v1/travel-times", "/api/v1/categories", "/api/v1/status"]);

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(STATIC).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("agenda-static-") && k !== STATIC).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/admin/")) return;
  if (request.mode === "navigate") {
    event.respondWith(page(event, url));
  } else if (REFERENCE.has(url.pathname)) {
    event.respondWith(networkFirst(event, request));
  } else if (PRECACHE.includes(url.pathname)) {
    event.respondWith(cacheFirst(request));
  }
});

async function page(event, url) {
  const request = event.request;
  try {
    const response = await fetch(request);
    if (response.ok && response.type === "basic") {
      if (url.pathname === SHELL) {
        // Any agenda visit refreshes the shell (and its footer's sync date).
        const copy = response.clone();
        event.waitUntil(caches.open(STATIC).then((c) => c.put(SHELL, copy)));
      } else if (url.pathname.startsWith("/evenements/")) {
        const copy = response.clone();
        event.waitUntil(caches.open(PAGES).then((c) => c.put(url.pathname, copy).then(() => trim(c))));
      }
    } else if ((response.status === 404 || response.status === 410) && url.pathname.startsWith("/evenements/")) {
      // Gone or withdrawn: never show the old copy offline.
      event.waitUntil(caches.open(PAGES).then((c) => c.delete(url.pathname)));
    }
    return response;
  } catch {
    const cached = (await caches.match(url.pathname, { ignoreSearch: true })) || (await caches.match(SHELL));
    return cached || Response.error();
  }
}

async function trim(cache) {
  const keys = await cache.keys();
  await Promise.all(keys.slice(0, Math.max(0, keys.length - MAX_PAGES)).map((k) => cache.delete(k)));
}

async function networkFirst(event, request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const copy = response.clone();
      event.waitUntil(caches.open(STATIC).then((c) => c.put(request, copy)));
    }
    return response;
  } catch {
    return (await caches.match(request)) || Response.error();
  }
}

async function cacheFirst(request) {
  return (await caches.match(request)) || fetch(request);
}
