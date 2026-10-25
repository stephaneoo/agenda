// An in-memory Cache API (CacheStorage and Cache) for the node tests of sw.js and store.js.
// Keys are absolute URLs; a path is resolved against `origin`.

export class FakeCache {
  constructor(origin) {
    this.origin = origin;
    this.entries = new Map();
  }

  key(request) {
    const url = typeof request === "string" ? request : request.url;
    const parsed = new URL(url, this.origin);
    return parsed.origin + parsed.pathname + parsed.search;
  }

  async match(request, { ignoreSearch = false } = {}) {
    const key = this.key(request);
    if (this.entries.has(key)) return this.entries.get(key).clone();
    if (ignoreSearch) {
      const bare = key.split("?")[0];
      for (const [k, v] of this.entries) if (k.split("?")[0] === bare) return v.clone();
    }
    return undefined;
  }

  async put(request, response) {
    this.entries.set(this.key(request), response);
  }

  async addAll(urls) {
    for (const url of urls) this.entries.set(this.key(url), new Response(`precached ${url}`));
  }

  async delete(request) {
    return this.entries.delete(this.key(request));
  }

  async keys() {
    return [...this.entries.keys()].map((url) => ({ url }));
  }
}

export class FakeCaches {
  constructor(origin = "https://site.test") {
    this.origin = origin;
    this.caches = new Map();
  }

  async open(name) {
    if (!this.caches.has(name)) this.caches.set(name, new FakeCache(this.origin));
    return this.caches.get(name);
  }

  async match(request, options) {
    for (const cache of this.caches.values()) {
      const found = await cache.match(request, options);
      if (found) return found;
    }
    return undefined;
  }

  async keys() {
    return [...this.caches.keys()];
  }

  async delete(name) {
    return this.caches.delete(name);
  }
}

export function page(body, headers = {}) {
  return new Response(`<h1>${body}</h1>`, { status: 200, headers: { "Content-Type": "text/html", ...headers } });
}
