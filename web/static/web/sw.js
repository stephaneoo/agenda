// The service worker, served at /sw.js (scope /) by web.views.service_worker, which fills in
// VERSION (a hash of the files it precaches), PRECACHE and KILL.
//
// - Precaches the app shell ("/"), the CSS and JS, and the reference endpoints.
// - Static files: from the cache (it is replaced whenever VERSION changes).
// - Reference endpoints: the network first, the cache when offline. A fetch with
//   cache: "no-store" (app.js asking whether the server is there) is left to the network.
// - Pages: the network first; any "/" visit refreshes the shell, visited occurrence pages
//   are kept (stamped with when), and dropped when the server answers 404 or 410, when they
//   are over MAX_AGE_DAYS old, or when app.js saves a copy that no longer lists them.
//   Offline, a page comes from the cache, else the shell, which renders from IndexedDB.
// - /api/v1/occurrences is never cached here: app.js keeps its copy in IndexedDB.
// - KILL (AGENDA_SW_KILL=1 on the server): the worker deletes every cache of the site,
//   unregisters itself and handles nothing.

const VERSION = "__VERSION__";
const PRECACHE = __PRECACHE__;
const KILL = __KILL__;
const SHELL = "/";
const STATIC = `agenda-static-${VERSION}`;
const PAGES = "agenda-pages";
const MAX_PAGES = 200;
const MAX_AGE_DAYS = 30;
const STAMP = "X-Agenda-Cached-At"; // ms since the epoch, on each cached page
const REFERENCE = new Set(["/api/v1/communes", "/api/v1/travel-times", "/api/v1/categories", "/api/v1/status"]);

const tooOld = (response, now = Date.now()) => {
  const at = Number(response.headers.get(STAMP));
  return !Number.isFinite(at) || at <= 0 || now - at > MAX_AGE_DAYS * 86_400_000;
};

async function stamped(response) {
  const headers = new Headers(response.headers);
  headers.set(STAMP, String(Date.now()));
  return new Response(await response.blob(), { status: response.status, statusText: response.statusText, headers });
}

async function killed() {
  await Promise.all((await caches.keys()).map((k) => caches.delete(k)));
  await self.registration.unregister();
}

self.addEventListener("install", (event) => {
  if (KILL) {
    event.waitUntil(self.skipWaiting());
    return;
  }
  event.waitUntil(caches.open(STATIC).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  if (KILL) {
    event.waitUntil(killed());
    return;
  }
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("agenda-static-") && k !== STATIC).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  if (KILL) return;
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/admin/")) return;
  if (request.mode === "navigate") {
    event.respondWith(page(event, url));
  } else if (REFERENCE.has(url.pathname)) {
    if (request.cache === "no-store") return;
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
        event.waitUntil(stamped(copy).then((s) => caches.open(PAGES).then((c) => c.put(url.pathname, s).then(() => trim(c)))));
      }
    } else if ((response.status === 404 || response.status === 410) && url.pathname.startsWith("/evenements/")) {
      // Gone or withdrawn: never show the old copy offline.
      event.waitUntil(caches.open(PAGES).then((c) => c.delete(url.pathname)));
    }
    return response;
  } catch {
    const pages = await caches.open(PAGES);
    let cached = await pages.match(url.pathname, { ignoreSearch: true });
    if (cached && tooOld(cached)) {
      await pages.delete(url.pathname);
      cached = undefined;
    }
    cached = cached || (await caches.match(SHELL));
    return cached || Response.error();
  }
}

async function trim(cache) {
  const keys = await cache.keys();
  const now = Date.now();
  const old = [];
  for (const k of keys) {
    const r = await cache.match(k);
    if (!r || tooOld(r, now)) old.push(k);
  }
  const fresh = keys.filter((k) => !old.includes(k));
  const excess = fresh.slice(0, Math.max(0, fresh.length - MAX_PAGES));
  await Promise.all([...old, ...excess].map((k) => cache.delete(k)));
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
