---
status: accepted
date: 2026-10-25
decision-makers: Pierre
---

# Per-source claims, with a precomputed canonical projection

## Context and Problem Statement

Events arrive from several sources: DATAtourisme first, then direct submissions, agents, and possibly Meta mentions. Moderators also edit and hide events. Moderation decisions must survive the nightly sync. Later, the same event may arrive from two sources. How is the data stored so that provenance, merging and moderation all hold up?

## Considered Options

* One merged record; the sync overwrites it; our edits are locked fields
* Merged record, plus the raw payload of each source
* Per-source claims, with the canonical record computed from them

## Decision Outcome

Chosen option: "Per-source claims", with reads served from a **precomputed** canonical projection. Every source, including moderation, writes claims `(target, field, value, source, seen_at)`. Writing claims recomputes the canonical rows of the affected event in the same transaction. All reads (pages, API, offline export) query only the canonical tables.

### Consequences

* Good, because a moderator's edit or hide is just a higher-priority claim, so the nightly sync can't undo it.
* Good, because provenance for every field comes for free, and a second source is a new claim producer, not a schema change.
* Bad, because every write pays for the recomputation, and the projection logic has to be deterministic.
* Bad, because Django admin can't edit the canonical tables directly. Admin edits have to be turned into moderation claims.

### Confirmation

* A test rebuilds the whole projection from claims and checks that it equals the projection maintained incrementally.
* A test shows that a moderator hiding an event, followed by a sync of a changed source record, leaves the event hidden and its other fields updated.

## Hard constraints

* Canonical tables are written only by the projection code, never directly.
* The projection is a pure function of the claims. A full rebuild command exists.
* Reads never compute the projection on the fly.
