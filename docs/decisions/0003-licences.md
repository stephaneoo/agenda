---
status: accepted
date: 2026-10-25
decision-makers: Pierre
---

# Licences: AGPL code, Licence Ouverte data, reserved branding

## Context and Problem Statement

The project republishes data from DATAtourisme, which is under Licence Ouverte / Etalab 2.0 (no share-alike), and adds its own data: direct submissions and curation. Its code runs as a network service. Under which terms are the code, the data and the brand published?

## Considered Options

* Code: AGPL-3.0, MIT
* Data: Licence Ouverte 2.0, ODbL (share-alike)
* Branding: under the code licence, or reserved

## Decision Outcome

Chosen option: code under **AGPL-3.0-or-later**, data under **Licence Ouverte / Etalab 2.0**, and the **name and logo reserved**, not covered by either licence.

### Consequences

* Good, because anyone running a modified version as a service must share their changes.
* Good, because the data stays as reusable as its main source, and can flow back to Apidae and DATAtourisme.
* Good, because a fork can reuse the code and data but not pass itself off as the project.
* Bad, because Licence Ouverte doesn't require reusers to keep the data open.

### Confirmation

* `LICENSE` is the AGPL text. A notice in the repo states the data licence and the branding reservation.
* Every page, feed and API response that carries DATAtourisme data cites the source and its last update date.

## Hard constraints

* Never ingest an ODbL source without revisiting this decision, because its share-alike would carry over to the merged dataset.
* Images keep their own copyright and are not covered by the data licence.
