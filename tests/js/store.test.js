// The offline copy in IndexedDB, on fake-indexeddb (FAKE_INDEXEDDB: its unpacked npm package,
// set by the dev shell and the flake check).
import { test } from "node:test";
import assert from "node:assert/strict";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import { loadSnapshot, saveSnapshot } from "../../web/static/web/store.js";

const dir = process.env.FAKE_INDEXEDDB;
if (!dir) throw new Error("FAKE_INDEXEDDB is not set: run the tests in the dev shell");
const { IDBFactory } = await import(pathToFileURL(join(dir, "build/esm/index.js")).href);

const occs = [
  { id: "occ_37-VxVS-xkky", name: "Exposition", start: "2026-10-01", end: "2026-12-31", location: { commune: "26220" } },
  { id: "occ_ILFC_PoSPj1F", name: "Marché", start: "2026-11-05", end: null, location: { commune: "26220" } },
];

test("an empty store has no snapshot", async () => {
  assert.equal(await loadSnapshot({ factory: new IDBFactory() }), null);
});

test("a snapshot round-trips, with when it was synced and saved", async () => {
  const factory = new IDBFactory();
  const now = new Date("2026-10-26T07:15:00Z");
  assert.equal(await saveSnapshot(occs, "2026-10-26T06:02:11+01:00", { factory, now }), true);
  const snap = await loadSnapshot({ factory });
  assert.deepEqual(snap.occurrences, occs);
  assert.equal(snap.syncedAt, "2026-10-26T06:02:11+01:00");
  assert.equal(snap.savedAt, now.toISOString());
});

test("a new snapshot replaces the previous one", async () => {
  const factory = new IDBFactory();
  await saveSnapshot(occs, null, { factory });
  await saveSnapshot(occs.slice(1), "2026-10-27T06:01:00+01:00", { factory });
  const snap = await loadSnapshot({ factory });
  assert.deepEqual(snap.occurrences.map((o) => o.name), ["Marché"]);
  assert.equal(snap.syncedAt, "2026-10-27T06:01:00+01:00");
});

test("without IndexedDB (private browsing, old browser) nothing throws", async () => {
  assert.equal(await loadSnapshot({ factory: null }), null);
  assert.equal(await saveSnapshot(occs, null, { factory: null }), false);
});
