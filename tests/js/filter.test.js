// The offline filter answers like agenda.read.search_occurrences (tests/agenda/test_read.py
// runs the same cases against the database), except that it doesn't paginate.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { InvalidQuery, filterOccurrences, isSpanning, parisToday, periodRange } from "../../web/static/web/filter.js";

const fixture = JSON.parse(readFileSync(new URL("../fixtures/filter-cases.json", import.meta.url), "utf8"));
// As GET /api/v1/occurrences serves them (only the fields the filter reads, plus name).
const served = fixture.occurrences.map((o) => ({
  id: o.id, name: o.name, start: o.start, end: o.end, category: o.category, location: o.location,
}));
const context = { today: fixture.today, slugs: fixture.slugs };

function shuffled(items) {
  // A fixed permutation: the filter must sort, whatever order the copy is in.
  return [...items].reverse().sort((a, b) => (a.id.charCodeAt(5) % 3) - (b.id.charCodeAt(5) % 3));
}

function query(q) {
  return { commune: q.commune ?? null, within: q.within ?? null, from: q.from ?? null, to: q.to ?? null,
           categories: q.categories ?? [] };
}

for (const c of fixture.cases) {
  test(c.name, () => {
    if (c.error) {
      assert.throws(() => filterOccurrences(served, fixture.matrix, query(c.query), context),
        (e) => e instanceof InvalidQuery && e.message.includes(c.error));
    } else {
      const got = filterOccurrences(shuffled(served), fixture.matrix, query(c.query), context);
      assert.deepEqual(got.map((o) => o.name), c.expected);
    }
  });
}

test("period choices give the same dates as the server", () => {
  for (const p of fixture.periods) {
    assert.deepEqual(periodRange(p.period, p.today), { from: p.from, to: p.to }, `${p.period} ${p.today}`);
  }
  assert.deepEqual(periodRange("inconnue", "2026-11-04"), periodRange("7j", "2026-11-04"));
});

test("today is the date in Paris", () => {
  // 23:30 UTC on 31 October is already 1 November in Paris.
  assert.equal(parisToday(new Date("2026-10-31T23:30:00Z")), "2026-11-01");
  assert.equal(parisToday(new Date("2026-07-01T21:59:00Z")), "2026-07-01");
});

test("spanning: more than 24 hours on the wall clock", () => {
  assert.equal(isSpanning({ start: "2026-10-01", end: "2026-12-31" }), true);
  assert.equal(isSpanning({ start: "2026-11-05", end: null }), false);
  assert.equal(isSpanning({ start: "2026-10-25", end: null }), false); // a 25-hour day is still one day
  assert.equal(isSpanning({ start: "2026-11-04T21:00:00+01:00", end: "2026-11-05T01:00:00+01:00" }), false);
  assert.equal(isSpanning({ start: "2026-11-04", end: "2026-11-05" }), true);
});
