// The offline filter: the same answers as agenda.read.search_occurrences, without pagination
// (tests/fixtures/filter-cases.json holds the cases both are tested on).
//
// Dates are "YYYY-MM-DD" strings, starts and ends what the API serves: a local date, or an
// RFC 3339 date-time with the Europe/Paris offset. An occurrence matches a range when
// start < (to + 1 day) 00:00 and its last instant >= from 00:00, local time in Paris; a date
// runs to 24:00, a date-time end stops just before it. Results sort by start, then by id in
// plain string order, which is the server's byte order (ids are ASCII).

export const MAX_WITHIN = 60;
export const MAX_SPAN_DAYS = 92;
export const DEFAULT_SPAN_DAYS = 30;
export const PERIODS = ["aujourdhui", "week-end", "7j", "30j"];
export const DEFAULT_PERIOD = "7j";

const INSEE = /^[0-9][0-9AB][0-9]{3}$/;
const DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const DAY_MS = 86400000;
// The server's tick is 1 µs; with starts and ends to the second, 1 ms gives the same answers.
const TICK = 1;

export class InvalidQuery extends Error {
  constructor(detail) {
    super(detail);
    this.name = "InvalidQuery";
    this.detail = detail;
  }
}

// --- Calendar dates (as UTC day numbers, so no local time zone leaks in) --------------------

function dayNumber(s) {
  const m = typeof s === "string" ? DATE.exec(s) : null;
  if (!m) return null;
  const t = Date.UTC(+m[1], +m[2] - 1, +m[3]);
  const d = new Date(t);
  return d.getUTCFullYear() === +m[1] && d.getUTCMonth() === +m[2] - 1 && d.getUTCDate() === +m[3] ? t : null;
}

function isoDate(t) {
  return new Date(t).toISOString().slice(0, 10);
}

export function addDays(s, n) {
  return isoDate(dayNumber(s) + n * DAY_MS);
}

function weekday(s) {
  return (new Date(dayNumber(s)).getUTCDay() + 6) % 7; // Monday is 0, as in Python
}

// --- Paris time ------------------------------------------------------------------------------

const PARIS = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Paris", hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit",
});

// The wall-clock time in Paris at an instant, as milliseconds of a UTC date with that wall time.
export function parisWall(ms) {
  const p = {};
  for (const part of PARIS.formatToParts(new Date(ms))) p[part.type] = part.value;
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
}

export function parisToday(now = new Date()) {
  return isoDate(parisWall(now.getTime()));
}

// The instant of local midnight in Paris (it always exists: the changes are at 02:00 and 03:00).
export function dayStart(s) {
  const wall = dayNumber(s);
  let t = wall - (parisWall(wall) - wall);
  t = wall - (parisWall(t) - t);
  return t;
}

const isDate = (s) => s.length === 10;
const instant = (s) => (isDate(s) ? dayStart(s) : Date.parse(s));

// [first instant, last instant included], as agenda.timeutil.bounds.
export function bounds(start, end) {
  const first = instant(start);
  let last;
  if (end == null) last = isDate(start) ? dayStart(addDays(start, 1)) - TICK : first;
  else if (!isDate(end)) last = Math.max(first, Date.parse(end) - TICK);
  else last = dayStart(addDays(end, 1)) - TICK;
  return [first, last];
}

// --- Query -----------------------------------------------------------------------------------

// The communes `within` minutes of `origin`, origin included; 0 or none: the origin alone.
export function reachable(matrix, origin, within) {
  if (!within) return new Set([origin]);
  const i = matrix.communes.indexOf(origin);
  const out = new Set([origin]);
  matrix.communes.forEach((insee, j) => {
    const m = i < 0 ? null : matrix.minutes[i][j];
    if (m !== null && m !== undefined && m <= within) out.add(insee);
  });
  return out;
}

function validDate(value, name) {
  if (value == null || value === "") return null;
  if (dayNumber(value) === null) throw new InvalidQuery(`${name} is not a date (YYYY-MM-DD)`);
  return value;
}

// The checks of GET /api/v1/occurrences, with the server's messages.
export function validate(matrix, { commune = null, within = null, from = null, to = null, categories = [] }, context = {}) {
  if (within !== null && within !== undefined && within !== "") {
    within = Number(within);
    if (!commune) throw new InvalidQuery("within requires commune");
    if (!Number.isInteger(within) || within < 0 || within > MAX_WITHIN) {
      throw new InvalidQuery(`within must be between 0 and ${MAX_WITHIN}`);
    }
  } else {
    within = null;
  }
  let communes = null;
  if (commune) {
    if (!INSEE.test(commune)) throw new InvalidQuery("commune must be an INSEE code");
    if (!matrix.communes.includes(commune)) throw new InvalidQuery(`commune ${commune} is not in the covered area`);
    communes = reachable(matrix, commune, within || 0);
  }
  from = validDate(from, "from") || context.today || parisToday();
  to = validDate(to, "to") || addDays(from, DEFAULT_SPAN_DAYS);
  const span = Math.round((dayNumber(to) - dayNumber(from)) / DAY_MS);
  if (span < 0) throw new InvalidQuery("to is before from");
  if (span > MAX_SPAN_DAYS) throw new InvalidQuery(`the date range spans more than ${MAX_SPAN_DAYS} days`);
  if (context.slugs) {
    const unknown = [...new Set(categories)].filter((c) => !context.slugs.includes(c)).sort();
    if (unknown.length) throw new InvalidQuery(`unknown category: ${unknown.join(", ")}`);
  }
  return { communes, from, to, categories: [...new Set(categories)] };
}

// The matching occurrences, sorted by start then id. `context`: {today, slugs}, both optional.
export function filterOccurrences(occs, matrix, query, context = {}) {
  const q = validate(matrix, query, context);
  const lo = dayStart(q.from);
  const hi = dayStart(addDays(q.to, 1));
  const categories = q.categories.length ? new Set(q.categories) : null;
  return occs
    .map((o) => [o, bounds(o.start, o.end)])
    .filter(([o, [first, last]]) =>
      first < hi && last >= lo
      && (!q.communes || q.communes.has(o.location && o.location.commune))
      && (!categories || categories.has(o.category)))
    .sort(([a, [fa]], [b, [fb]]) => fa - fb || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
    .map(([o]) => o);
}

// --- The agenda page's choices and grouping (web.views) --------------------------------------

export function periodRange(period, today) {
  if (period === "aujourdhui") return { from: today, to: today };
  if (period === "week-end") {
    const wd = weekday(today);
    const saturday = wd < 5 ? addDays(today, (5 - wd) % 7) : today;
    return { from: saturday, to: addDays(today, 6 - wd) };
  }
  return { from: today, to: addDays(today, period === "30j" ? 29 : 6) };
}

function wall(value, isEnd) {
  if (isDate(value)) return dayNumber(value) + (isEnd ? DAY_MS : 0);
  return parisWall(Date.parse(value));
}

// More than 24 h on the wall clock: shown once under "En cours", not under each day.
export function isSpanning(o) {
  const start = wall(o.start, false);
  const end = o.end ? wall(o.end, true) : isDate(o.start) ? wall(o.start, true) : start;
  return end - start > DAY_MS;
}

// {ongoing: [occ], days: [{day, rows: [occ]}]}: the agenda page's groups for a sorted list.
export function groupForList(occs, from) {
  const ongoing = [];
  const days = new Map();
  for (const o of occs) {
    if (isSpanning(o)) {
      ongoing.push(o);
      continue;
    }
    let day = isoDate(parisWall(instant(o.start)));
    if (day < from) day = from;
    if (!days.has(day)) days.set(day, []);
    days.get(day).push(o);
  }
  return { ongoing, days: [...days.keys()].sort().map((day) => ({ day, rows: days.get(day) })) };
}

// --- Presentation ------------------------------------------------------------------------------

// The site path of an occurrence URL, or null: only an occurrence page ("/evenements/…") on
// one of `origins` (the page's own, and the configured site's) is ever made a link.
export function pathOf(url, origins) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  return origins.has(parsed.origin) && parsed.pathname.startsWith("/evenements/") ? parsed.pathname : null;
}

export const STATUS_LABELS = new Map([["rescheduled", "Reprogrammé"], ["postponed", "Reporté"], ["cancelled", "Annulé"]]);

// Label tables are Maps: a value such as "__proto__" or "constructor" is just a missing key.
export const statusLabel = (status) => STATUS_LABELS.get(status) ?? null;
export const categoryLabels = (categories) => new Map(categories.map((c) => [c.slug, c.label]));
