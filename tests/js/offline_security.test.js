// Batch C security review: links only to the site's occurrence pages, label lookups that
// can't reach Object.prototype, cached pages dropped when the new copy no longer lists them
// or they are too old, and no re-download of a fresh copy.
import { test } from "node:test";
import assert from "node:assert/strict";

import { categoryLabels, pathOf, statusLabel } from "../../web/static/web/filter.js";
import { FRESH_MS, evictPages, isFresh } from "../../web/static/web/store.js";
import { FakeCaches, page } from "./fake_caches.js";

const ORIGIN = "https://site.test";
const ORIGINS = new Set([ORIGIN, "https://agenda.example.org"]);
const DAY = 86_400_000;

test("pathOf links only to /evenements/ on the site's own origins", () => {
  assert.equal(pathOf(`${ORIGIN}/evenements/marche-x-2026-11-05/`, ORIGINS), "/evenements/marche-x-2026-11-05/");
  assert.equal(pathOf("https://agenda.example.org/evenements/a-2026-11-05/?x=1#y", ORIGINS), "/evenements/a-2026-11-05/");
  for (const url of [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "https://evil.example/evenements/x-2026-11-05/",
    "http://site.test/evenements/x-2026-11-05/",
    `${ORIGIN}/admin/`,
    `${ORIGIN}/`,
    "/evenements/relative-2026-11-05/",
    "//evil.example/evenements/x/",
    "not a url",
    null,
    { toString: () => "javascript:alert(1)" },
  ]) {
    assert.equal(pathOf(url, ORIGINS), null, String(url));
  }
});

test("label lookups are Maps: __proto__ or constructor is just unknown", () => {
  assert.equal(statusLabel("cancelled"), "Annulé");
  for (const s of ["constructor", "__proto__", "toString", "hasOwnProperty", undefined, null]) assert.equal(statusLabel(s), null);
  const labels = categoryLabels([{ slug: "marche", label: "Marché" }]);
  assert.equal(labels.get("marche"), "Marché");
  assert.equal(labels.get("__proto__"), undefined);
  assert.equal(labels.get("constructor"), undefined);
});

test("evictPages drops pages the new copy doesn't list, and pages over 30 days", async () => {
  const caches = new FakeCaches(ORIGIN);
  const pages = await caches.open("agenda-pages");
  const now = Date.now();
  const stamp = (age) => ({ "X-Agenda-Cached-At": String(now - age) });
  await pages.put("/evenements/listed-2026-11-05/", page("listed", stamp(DAY)));
  await pages.put("/evenements/hidden-2026-11-05/", page("hidden", stamp(DAY)));
  await pages.put("/evenements/old-2026-11-05/", page("old", stamp(31 * DAY)));
  await pages.put("/evenements/unstamped-2026-11-05/", page("unstamped"));
  const keep = new Set(["/evenements/listed-2026-11-05/", "/evenements/old-2026-11-05/", "/evenements/unstamped-2026-11-05/"]);
  assert.equal(await evictPages(keep, { cachesApi: caches, now }), 3);
  assert.deepEqual((await pages.keys()).map((k) => new URL(k.url).pathname), ["/evenements/listed-2026-11-05/"]);
  assert.equal(await evictPages(keep, { cachesApi: undefined }), 0); // no Cache API: nothing throws
});

test("a stored copy is fresh under 10 minutes, complete, from the same sync", () => {
  const now = new Date("2026-11-01T09:00:00Z");
  const saved = (msAgo, extra = {}) => ({
    occurrences: [], syncedAt: "2026-10-31T06:02:00+01:00", savedAt: new Date(now - msAgo).toISOString(), complete: true, ...extra,
  });
  assert.equal(isFresh(saved(60_000), "2026-10-31T06:02:00+01:00", now), true);
  assert.equal(isFresh(saved(FRESH_MS + 1), "2026-10-31T06:02:00+01:00", now), false); // too old
  assert.equal(isFresh(saved(60_000), "2026-11-01T06:01:00+01:00", now), false); // a new sync
  assert.equal(isFresh(saved(60_000, { complete: false }), "2026-10-31T06:02:00+01:00", now), false);
  assert.equal(isFresh(saved(-60_000), "2026-10-31T06:02:00+01:00", now), false); // saved "in the future"
  assert.equal(isFresh(saved(60_000), undefined, now), false); // status unavailable
  assert.equal(isFresh(saved(60_000, { syncedAt: null }), null, now), true);
  assert.equal(isFresh(null, null, now), false);
});
