// The JS island: an enhancement only. Without it the server-rendered filters work as they are.
//
// On each visit: register the service worker, and download the covered area's published
// occurrences for the next 30 days into IndexedDB. With that copy, the agenda's filters run
// here, instantly, online or not. Offline, a banner says when the copy was saved; the agenda
// renders from the copy, and an occurrence page that wasn't cached renders from it too.

import { InvalidQuery, addDays, filterOccurrences, groupForList, parisToday, periodRange } from "./filter.js";
import { bannerText, loadSnapshot, saveSnapshot } from "./store.js";

const API = "/api/v1";
const SPAN_DAYS = 30;
const PAGE_LIMIT = 200;
const MAX_PAGES = 25;
const STATUS_LABELS = { rescheduled: "Reprogrammé", postponed: "Reporté", cancelled: "Annulé" };
const TZ = "Europe/Paris";

const fmt = (options) => new Intl.DateTimeFormat("fr-FR", { timeZone: TZ, ...options });
const DAY_HEADING = fmt({ weekday: "long", day: "numeric", month: "long" });
const SHORT_DAY = fmt({ day: "numeric", month: "short" });
const LONG_DATE = fmt({ weekday: "long", day: "numeric", month: "long", year: "numeric" });
const HOUR = fmt({ hour: "2-digit", minute: "2-digit" });
const DAY_ONLY = fmt({ day: "numeric", month: "long", year: "numeric" });

// A local date as a Date at noon UTC, so formatting it in Paris gives the same day.
const asDay = (s) => new Date(`${s}T12:00:00Z`);
const hasTime = (s) => s.length > 10;

// The server answered, with an error: not the same as being offline.
class HttpError extends Error {}

async function getJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new HttpError(`${url}: HTTP ${response.status}`);
  return response.json();
}

async function download() {
  const from = parisToday();
  const base = `${API}/occurrences?from=${from}&to=${addDays(from, SPAN_DAYS)}&limit=${PAGE_LIMIT}`;
  const items = [];
  let url = base;
  for (let i = 0; url && i < MAX_PAGES; i++) {
    const page = await getJson(url);
    items.push(...page.items);
    url = page.next ? `${base}&cursor=${encodeURIComponent(page.next)}` : null;
  }
  const complete = url === null; // else the cap stopped it: say the copy is partial
  const status = await getJson(`${API}/status`).catch(() => null);
  const syncedAt = status ? status.last_sync : null;
  const now = new Date();
  await saveSnapshot(items, syncedAt, { now, complete });
  return { occurrences: items, syncedAt, savedAt: now.toISOString(), complete };
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

const pathOf = (url) => new URL(url, location.origin).pathname;

function showBanner(snapshot, reason) {
  const banner = document.getElementById("offline-banner");
  if (!banner) return;
  banner.textContent = bannerText(snapshot, reason);
  banner.hidden = false;
}

// --- Agenda ----------------------------------------------------------------------------------

function row(o, spanning, labels) {
  let time = "journée";
  if (spanning && o.end) time = `jusqu'au ${SHORT_DAY.format(hasTime(o.end) ? new Date(o.end) : asDay(o.end))}`;
  else if (hasTime(o.start)) time = HOUR.format(new Date(o.start));
  return el("li", {},
    el("span", { class: "time", text: time }),
    el("span", { class: "cat", text: labels[o.category] || o.category }),
    el("a", { class: "title", href: pathOf(o.url), text: o.name }),
    el("span", { class: "commune", text: o.location.commune_name }),
    STATUS_LABELS[o.status] ? el("span", { class: `status status-${o.status}`, text: STATUS_LABELS[o.status] }) : null);
}

function section(title, rows) {
  return el("section", { class: "day" }, el("h2", { text: title }), el("ul", { class: "rows" }, ...rows));
}

function renderList(results, occs, from, labels) {
  const { ongoing, days } = groupForList(occs, from);
  const parts = [];
  if (!occs.length) parts.push(el("p", { class: "empty", text: "Aucun événement pour ces critères." }));
  for (const d of days) parts.push(section(DAY_HEADING.format(asDay(d.day)), d.rows.map((o) => row(o, false, labels))));
  // As the server renders it: after the days, collapsed.
  if (ongoing.length) {
    parts.push(el("details", { class: "ongoing" },
      el("summary", { text: `En cours / toute la période (${ongoing.length})` }),
      el("ul", { class: "rows" }, ...ongoing.map((o) => row(o, true, labels)))));
  }
  results.replaceChildren(...parts);
}

function readForm(form) {
  const data = new FormData(form);
  return {
    commune: data.get("commune") || null,
    within: data.get("rayon") === null ? null : Number(data.get("rayon")),
    period: data.get("periode") || "7j",
    categories: data.getAll("categorie"),
  };
}

// Set the form from the URL, as the server would have (the shell may be served for any URL).
function applyUrl(form) {
  const params = new URLSearchParams(location.search);
  for (const name of ["commune", "rayon", "periode"]) {
    const select = form.elements[name];
    if (select && params.has(name) && [...select.options].some((o) => o.value === params.get(name))) {
      select.value = params.get(name);
    }
  }
  if (params.has("categorie")) {
    const wanted = new Set(params.getAll("categorie"));
    for (const box of form.querySelectorAll('input[name="categorie"]')) box.checked = wanted.has(box.value);
  }
}

function enhanceAgenda(state, offline) {
  const form = document.getElementById("filters");
  const results = document.getElementById("results");
  if (!form || !results) return;
  const labels = Object.fromEntries(state.categories.map((c) => [c.slug, c.label]));
  const slugs = state.categories.map((c) => c.slug);
  const update = (replaceUrl) => {
    const f = readForm(form);
    const { from, to } = periodRange(f.period, parisToday());
    let occs;
    try {
      occs = filterOccurrences(state.snapshot.occurrences, state.matrix,
        { commune: f.commune, within: f.within, from, to, categories: f.categories }, { slugs });
    } catch (e) {
      if (e instanceof InvalidQuery && !offline) form.submit(); // let the server answer
      return;
    }
    renderList(results, occs, from, labels);
    if (replaceUrl) history.replaceState(null, "", `/?${new URLSearchParams(new FormData(form))}`);
  };
  form.addEventListener("change", () => update(true));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    update(true);
  });
  if (offline) {
    applyUrl(form);
    update(false);
  }
}

// --- Occurrence page, from the copy ------------------------------------------------------------

function renderOccurrence(snapshot, categories) {
  const main = document.querySelector("main");
  if (!main) return;
  const o = snapshot && snapshot.occurrences.find((x) => pathOf(x.url) === location.pathname);
  if (!o) {
    main.replaceChildren(el("p", { class: "empty", text: "Cette page n'a pas été enregistrée pour une consultation hors ligne." }),
      el("p", {}, el("a", { href: "/", text: "Retour à l'agenda" })));
    return;
  }
  const label = (categories.find((c) => c.slug === o.category) || {}).label || o.category;
  const when = (s) => (hasTime(s) ? `${LONG_DATE.format(new Date(s))}, ${HOUR.format(new Date(s))}` : LONG_DATE.format(asDay(s)));
  const loc = o.location;
  const place = [loc.name, loc.street, `${loc.postal_code} ${loc.commune_name}`].filter(Boolean).join(", ");
  document.title = o.name;
  main.replaceChildren(el("article", { class: "occurrence" },
    el("h1", { text: o.name }),
    STATUS_LABELS[o.status] ? el("p", { class: `status status-${o.status}`, text: STATUS_LABELS[o.status] }) : null,
    el("dl", {},
      el("dt", { text: "Date" }), el("dd", { text: o.end ? `${when(o.start)} – ${when(o.end)}` : when(o.start) }),
      el("dt", { text: "Lieu" }), el("dd", { text: place }),
      el("dt", { text: "Catégorie" }), el("dd", { text: label })),
    o.description ? el("div", { class: "description" }, ...o.description.split(/\n+/).map((p) => el("p", { text: p }))) : null,
    el("p", { class: "attribution", text: o.sources.map((s) =>
      `Source : ${s.name} (${s.licence}), mise à jour le ${DAY_ONLY.format(new Date(s.updated_at))}.`).join(" ") })));
}

// --- Start ---------------------------------------------------------------------------------------

async function main() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  const page = document.body.dataset.page;
  let snapshot;
  let offline = false;
  try {
    snapshot = await download();
    document.documentElement.dataset.offlineCopy = "saved"; // for the e2e check, and DevTools
  } catch (e) {
    offline = true;
    snapshot = await loadSnapshot();
    showBanner(snapshot, e instanceof HttpError ? "unavailable" : "offline");
  }
  let categories = [];
  let matrix = null;
  try {
    [categories, matrix] = await Promise.all([getJson(`${API}/categories`), getJson(`${API}/travel-times`)]);
  } catch {
    return; // no reference data, not even cached: the page stays as rendered
  }
  if (offline && location.pathname.startsWith("/evenements/") && page !== "occurrence") {
    renderOccurrence(snapshot, categories);
  } else if (page === "agenda" && snapshot) {
    enhanceAgenda({ snapshot, matrix, categories }, offline);
  }
}

main();
