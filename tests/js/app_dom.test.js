// app.js renders hostile occurrence data from IndexedDB offline: through a recording fake
// DOM, every value must land in text, never in an HTML sink or a javascript: attribute, and
// the service worker is not registered off the configured origin.
import { test } from "node:test";
import assert from "node:assert/strict";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const dir = process.env.FAKE_INDEXEDDB;
if (!dir) throw new Error("FAKE_INDEXEDDB is not set: run the tests in the dev shell");
const { IDBFactory } = await import(pathToFileURL(join(dir, "build/esm/index.js")).href);
globalThis.indexedDB = new IDBFactory();

const sinks = [];
const attrs = [];
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ""; this.dataset = {}; this.hidden = true; }
  set innerHTML(v) { sinks.push(["innerHTML", v]); }
  set outerHTML(v) { sinks.push(["outerHTML", v]); }
  insertAdjacentHTML(_, v) { sinks.push(["insertAdjacentHTML", v]); }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text; }
  setAttribute(k, v) { attrs.push([this.tag, k, v]); }
  append(...c) { this.children.push(...c); }
  replaceChildren(...c) { this.children = c; }
}
const main = new Node("main");
const metas = { "agenda-origin": "https://agenda.example.org" };
globalThis.document = {
  createElement: (t) => new Node(t),
  getElementById: () => null,
  querySelector: (s) => {
    if (s === "main") return main;
    const m = /^meta\[name="([^"]+)"\]$/.exec(s);
    return m && metas[m[1]] ? { getAttribute: () => metas[m[1]] } : null;
  },
  body: { dataset: { page: "" } },
  documentElement: { dataset: {} },
  write: (v) => sinks.push(["document.write", v]),
  set title(v) { this._title = v; },
  get title() { return this._title; },
};
globalThis.location = { origin: "https://site.test", pathname: "/evenements/x-2026-11-05/", search: "" };
const registered = [];
Object.defineProperty(globalThis, "navigator", {
  value: { serviceWorker: { register: async (...a) => registered.push(a) } }, configurable: true,
});

const H = `<img src=x onerror=alert(1)>"'><script>alert(2)</script>`;
const hostile = {
  id: "o", name: "N" + H, start: "2026-11-05", end: null, status: "constructor", category: "__proto__",
  url: "https://site.test/evenements/x-2026-11-05/", description: "D" + H + "\n\nP2" + H,
  location: { name: "L" + H, street: "S" + H, postal_code: "26110", commune_name: "C" + H },
  sources: [{ name: "S" + H, licence: "L" + H, updated_at: "2026-10-25T00:00:00Z", url: "javascript:alert(1)" }],
};
const { saveSnapshot } = await import("../../web/static/web/store.js");
await saveSnapshot([hostile], null);
// Offline: the API is unreachable, the reference data comes from the worker's cache.
globalThis.fetch = async (url) => {
  const u = String(url);
  if (u.includes("/occurrences") || u.includes("/status")) throw new TypeError("offline");
  return { ok: true, json: async () => (u.includes("categories") ? [{ slug: "marche", label: "Marché" }] : {}) };
};

test("hostile values go to text only, and no worker is registered off the site origin", async () => {
  await import("../../web/static/web/app.js");
  await new Promise((r) => setTimeout(r, 300));
  const texts = [];
  const walk = (n) => { if (n && n._text) texts.push(n._text); (n?.children || []).forEach(walk); };
  walk(main);
  assert.deepEqual(sinks, []);
  assert.ok(texts.some((t) => t === "N" + H), "the name is rendered, as text");
  assert.equal(document.title, "N" + H);
  assert.ok(texts.includes("__proto__"), "an unknown category shows as its slug");
  assert.ok(!attrs.some(([, , v]) => /javascript:|onerror/i.test(String(v))), JSON.stringify(attrs));
  assert.ok(!attrs.some(([, k, v]) => k === "class" && String(v).includes("status")), "no status label for 'constructor'");
  assert.deepEqual(registered, []);
});
