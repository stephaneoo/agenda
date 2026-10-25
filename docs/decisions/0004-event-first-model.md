---
status: accepted
date: 2026-10-25
decision-makers: Pierre
---

# The event is the primary record; occurrences are derived

## Context and Problem Statement

A weekly market is one record in DATAtourisme and OpenAgenda, but 52 events in Google's one-event-per-page model. Which one is stored as the source of truth: the event, or each occurrence?

## Considered Options

* Event-first: an event with its dates; occurrences derived from it
* Occurrence-first: each date is a record; series is an optional grouping

## Decision Outcome

Chosen option: "Event-first", like DATAtourisme, which is the first and main source. Editing a series is one edit. An imported event maps one-to-one onto a stored event.

### Consequences

* Good, because ingestion doesn't have to expand periods into independent records and then reconcile them.
* Bad, because a change to one date (cancel one Tuesday) needs an occurrence-level override.
* Bad, because occurrences need a stable identity derived from the event's dates. Without it, Google sees URLs churn on every re-import.

### Confirmation

* Re-importing an unchanged source record leaves every occurrence ID and URL unchanged.
* Cancelling one occurrence leaves the event's other occurrences untouched.

## Hard constraints

* Every occurrence has its own stable public URL, and that URL survives re-imports.
* Occurrence-level overrides (status, time) exist from v1.
