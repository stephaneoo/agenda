// The service worker (web/static/web/sw.js), run in a node vm with a fake Cache API: page
// caching and its 30-day limit, the status probe left to the network, and the kill switch.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeCaches, page } from "./fake_caches.js";

const ORIGIN = "https://site.test";
const SOURCE = readFileSync(new URL("../../web/static/web/sw.js", import.meta.url), "utf8");
const DAY = 86_400_000;

function basic(response) {
  Object.defineProperty(response, "type", { value: "basic" });
  return response;
}

function load({ kill = false, network = async () => basic(page("network")) } = {}) {
  const code = SOURCE.replace('"__VERSION__"', '"v1"').replace("__PRECACHE__", '["/", "/static/web/app.js"]')
    .replace("__KILL__", JSON.stringify(kill));
  const handlers = {};
  const registration = { unregistered: false, async unregister() { this.unregistered = true; return true; } };
  const caches = new FakeCaches(ORIGIN);
  const self = {
    addEventListener: (type, fn) => { handlers[type] = fn; },
    location: { origin: ORIGIN },
    registration,
    skipWaiting: async () => {},
    clients: { claim: async () => {} },
  };
  vm.runInNewContext(code, { self, caches, fetch: (...a) => network(...a), Response, Headers, URL, Date, Number, String, Promise, Set });
  return { handlers, caches, registration };
}

async function lifecycle(sw, type) {
  const waits = [];
  sw.handlers[type]({ waitUntil: (p) => waits.push(p) });
  await Promise.all(waits);
}

async function request(sw, path, { mode = "navigate", cache = "default" } = {}) {
  let responded = null;
  const waits = [];
  sw.handlers.fetch({
    request: { method: "GET", url: ORIGIN + path, mode, cache },
    respondWith: (p) => { responded = p; },
    waitUntil: (p) => waits.push(p),
  });
  const response = responded && (await responded);
  await Promise.all(waits);
  return response;
}

const offline = async () => { throw new TypeError("Failed to fetch"); };

test("a visited occurrence page is cached, stamped, and served offline", async () => {
  const sw = load();
  await lifecycle(sw, "install");
  const before = Date.now();
  await request(sw, "/evenements/marche-x-2026-11-05/");
  const cached = await (await sw.caches.open("agenda-pages")).match("/evenements/marche-x-2026-11-05/");
  assert.ok(Number(cached.headers.get("X-Agenda-Cached-At")) >= before);
  const off = load({ network: offline });
  off.caches.caches = sw.caches.caches;
  const response = await request(off, "/evenements/marche-x-2026-11-05/");
  assert.equal(await response.text(), "<h1>network</h1>");
});

test("a cached page over 30 days old is dropped and the shell served instead", async () => {
  const sw = load({ network: offline });
  await lifecycle(sw, "install");
  const pages = await sw.caches.open("agenda-pages");
  await pages.put("/evenements/vieux-2026-09-01/", page("vieux", { "X-Agenda-Cached-At": String(Date.now() - 31 * DAY) }));
  await pages.put("/evenements/sans-date-2026-09-01/", page("sans date"));  // no stamp: too old too
  for (const path of ["/evenements/vieux-2026-09-01/", "/evenements/sans-date-2026-09-01/"]) {
    const response = await request(sw, path);
    assert.equal(await response.text(), "precached /");
    assert.equal(await pages.match(path), undefined);
  }
});

test("a 404 or 410 drops the cached page", async () => {
  const sw = load({ network: async () => basic(new Response("gone", { status: 410 })) });
  const pages = await sw.caches.open("agenda-pages");
  await pages.put("/evenements/x-2026-11-05/", page("x", { "X-Agenda-Cached-At": String(Date.now()) }));
  await request(sw, "/evenements/x-2026-11-05/");
  assert.equal(await pages.match("/evenements/x-2026-11-05/"), undefined);
});

test("the status probe (no-store) is left to the network", async () => {
  const sw = load({ network: offline });
  await lifecycle(sw, "install");
  assert.equal(await request(sw, "/api/v1/status", { mode: "cors", cache: "no-store" }), null);
  await (await sw.caches.open("agenda-static-v1")).put("/api/v1/status", new Response("{}"));
  assert.equal(await (await request(sw, "/api/v1/status", { mode: "cors" })).text(), "{}");
});

test("the kill switch clears every cache, unregisters, and handles nothing", async () => {
  const sw = load({ kill: true });
  for (const name of ["agenda-static-old", "agenda-pages", "other"]) await (await sw.caches.open(name)).put("/", page(name));
  await lifecycle(sw, "install");
  await lifecycle(sw, "activate");
  assert.deepEqual(await sw.caches.keys(), []);
  assert.equal(sw.registration.unregistered, true);
  assert.equal(await request(sw, "/evenements/x-2026-11-05/"), null);
  assert.equal(await request(sw, "/api/v1/communes", { mode: "cors" }), null);
});
