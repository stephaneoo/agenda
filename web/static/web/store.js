// The offline copy of the agenda, in IndexedDB: the covered area's published occurrences for
// the next 30 days, as GET /api/v1/occurrences serves them, with when the data was last synced
// upstream (syncedAt, from /api/v1/status), when this copy was saved (savedAt), and whether
// it is complete.
//
// Without IndexedDB (private browsing, blocked storage) nothing throws: saving answers false
// and loading null, and the page stays as the server rendered it.

const DB_NAME = "agenda-offline";
const DB_VERSION = 1;
const STORE = "snapshot";
const KEY = "occurrences";

function defaultFactory() {
  try {
    return globalThis.indexedDB || null;
  } catch {
    return null;
  }
}

function open(factory) {
  return new Promise((resolve, reject) => {
    const request = factory.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error("IndexedDB upgrade blocked"));
  });
}

function run(db, mode, action) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, mode);
    const request = action(tx.objectStore(STORE));
    tx.oncomplete = () => resolve(request.result);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("transaction aborted"));
  });
}

async function withDb(factory, fn) {
  const db = await open(factory);
  try {
    return await fn(db);
  } finally {
    db.close();
  }
}

// Replace the copy. Resolves true when saved.
// `complete`: false when the download stopped at its cap (the copy is partial).
export async function saveSnapshot(occs, syncedAt, { factory = defaultFactory(), now = new Date(), complete = true } = {}) {
  if (!factory) return false;
  const value = { occurrences: occs, syncedAt: syncedAt ?? null, savedAt: now.toISOString(), complete };
  try {
    await withDb(factory, (db) => run(db, "readwrite", (store) => store.put(value, KEY)));
    return true;
  } catch {
    return false;
  }
}

// {occurrences, syncedAt, savedAt}, or null when there is none.
export async function loadSnapshot({ factory = defaultFactory() } = {}) {
  if (!factory) return null;
  try {
    const value = await withDb(factory, (db) => run(db, "readonly", (store) => store.get(KEY)));
    return value && Array.isArray(value.occurrences) ? value : null;
  } catch {
    return null;
  }
}

const SAVED = new Intl.DateTimeFormat("fr-FR", {
  timeZone: "Europe/Paris", day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit",
});

// The banner shown when the page runs on the copy: "offline" (no network) or "unavailable"
// (the server answered with an error). Both dates, so stale data never looks current.
export function bannerText(snapshot, reason, format = (s) => SAVED.format(new Date(s))) {
  const prefix = reason === "unavailable" ? "Données momentanément indisponibles" : "Hors ligne";
  if (!snapshot) return `${prefix} · aucune copie enregistrée sur cet appareil`;
  const synced = snapshot.syncedAt ? `Données du ${format(snapshot.syncedAt)}` : "date de synchronisation inconnue";
  const partial = snapshot.complete === false ? " · copie partielle" : "";
  return `${prefix} · ${synced} · copie du ${format(snapshot.savedAt)}${partial}`;
}
