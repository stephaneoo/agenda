# Context: Nyonsais local events agenda

Input for the `/spec` skill, which will use it to bootstrap an empty repo, ask questions, write the spec, and then implement it test-first.

This file keeps four kinds of content apart: **constraints** (already decided), **leanings** (preferred but not decided), **findings** (researched, with a confidence level), and **open forks** (to settle during the spec run, sending architectural ones to `/madr`).

Date: 2026-10-25.

---

## 1. Vision

A simple page listing events around Nyons (Drôme provençale). A visitor picks a town and a radius expressed as **car travel time**, and gets the matching events.

The product has four properties:
- **API-first.** The UI is one client among others.
- **Offline-first.** It syncs opportunistically when there is a network connection.
- **Agent-first.** An MCP server gives agents the same access as humans.
- **Semantic.** Events are published as schema.org `Event` JSON-LD that conforms to Google's event structured data.

The hard problem is **feeding events**, not displaying them.

### Inspirations
- **https://www.agendamilitant.org/** (SPIP):
  - It is a dense chronological table where each row shows time, category icon, title and town.
  - It filters on two axes, the cause ("lutte") and the event type.
  - It has a "propose an event" form and a published moderation charter ("La Charte AMI").
  - It publishes an RSS feed and posts on a Mastodon account.
  - It has no location or radius filter.
- **https://bobine.art/** (La Bobine) is a JS app with an iOS app. Its content could not be read without JS, so *what to take from it is to be asked*.

---

## 2. Infrastructure and operations (bottom to top)

| Layer | Status | Notes |
|---|---|---|
| Host | **Constraint:** a single VM | Provider: leaning Scaleway, Hetzner possible. |
| Backups | **Constraint:** reliable, in another datacentre or region | See the finding below. |
| OS | Fork: NixOS vs Debian | Leaning NixOS for rollback. **A colleague will operate it with Claude.** |
| Storage | **Constraint:** ZFS for data, no encryption | No encryption keeps it simple for the colleague. |
| Database | **Constraint:** PostgreSQL | Add PostGIS. No counter-argument found. |
| App server | Fork: Django vs Rust vs PostgREST | See the findings. |
| Web server | Fork: Envoy vs nginx (vs Caddy) | Pierre would like to try Envoy. |
| Packaging | Fork: pure Nix vs docker compose | |
| Shipping | **Constraint:** git; the colleague deploys straight from his laptop to prod | |
| Dev loop | **Constraint:** develop on the laptop, commit once happy, then deploy | |
| Migrations | **Constraint:** zero-downtime, every breaking schema change is split over **two deploys** (expand, then contract) | |
| Admin | **Constraint:** an automatic admin area driven by the DB schema, an OpenAPI spec, and an admin front end | |
| Front end (JS/CSS) | Fork; Pierre wants advice | It is not his area. |

### Findings and positions on the stack

- **The app-server options are not interchangeable.**
  - **PostgREST** gives CRUD and OpenAPI from the schema. It does *not* run ingestion jobs (Apidae/DATAtourisme sync), Meta webhooks, the MCP server, or server-rendered pages for SEO, so choosing it means **PostgREST plus a second service**.
  - **Django** covers admin, API (DRF or django-ninja give OpenAPI), workers (management commands or cron) and server-side rendering in one codebase.
  - **Rust** provides no automatic admin, which conflicts with the admin constraint for v1.
- **PostgREST and the two-deploy rule.** With PostgREST the API *is* the schema. Expand/contract only works if the exposed API is a dedicated `api` schema of views over private tables. That extra layer has to be maintained.
- **Admin front end.** Django gives you its admin for free. With PostgREST, the usual route is react-admin with a PostgREST data provider; *its availability and maturity are not verified*.
- **Envoy on one VM** means writing Envoy's verbose static YAML config (listeners, clusters, filter chains) while using none of its strengths (dynamic config, mesh, many upstreams).
  - nginx is the battle-tested default.
  - Caddy (automatic TLS, short config) is the simplest option.
  - The real question is whether this is a learning exercise for Pierre or a service the colleague must keep running.
- **NixOS plus compose** means two packaging stories and two rollback stories. Two pairings are coherent: **NixOS with native modules** (nixpkgs ships `services.postgresql` and a PostgREST module), or **Debian with compose**.
  - The colleague's current Nix fluency matters more than rollback.
  - Claude writes Nix well, but someone new to Nix will find Nix evaluation errors harder to debug.
- **Backups.**
  - ZFS snapshots are good for **local** rollback.
  - Sending ZFS offsite needs a ZFS receiver, i.e. a second VM.
  - For the database, **pgBackRest or WAL-G to object storage in another region** gives point-in-time recovery and is cheaper.
  - Both Scaleway and Hetzner sell object storage in several regions.
  - A restore test should be part of the spec.
- **Front-end advice.**
  - Google wants one page per occurrence with JSON-LD, so **render pages on the server**.
  - **htmx conflicts with offline-first**, because every interaction is a server round-trip.
  - The default proposal is:
    - server-rendered HTML;
    - plain CSS or a light framework such as Pico.css;
    - a service worker plus IndexedDB for offline;
    - a small island of JS for the filter (vanilla, Alpine or Preact).
  - What "offline-first" means (see open forks) decides whether that is enough.
- **Car travel-time radius.**
  - Don't run a routing engine on every request.
  - Precompute a **commune × commune travel-time matrix** once with OSRM over OSM data and store it as a table. The area has a few dozen communes.
  - The matrix is small enough to ship to the client, so the filter also works offline.
  - Refresh it rarely.

---

## 3. Event data model: findings

These come from a survey of the formats; **no schema has been decided yet.**

### What each source exposes
- **schema.org `Event`** is the common ancestor.
  - Ticketing goes through `offers`.
  - Recurrence can be modelled as separate `Event` objects or with `eventSchedule`.
  - When an event is moved, `previousStartDate` is set together with `eventStatus`.
- **Google event rich results** use a strict subset of schema.org.
  - Required fields: `name`, `startDate`, `location`.
  - Google only supports **one event per page**, which in practice means one URL per occurrence.
- **ActivityPub.** The AS2 `Event` is minimal, and Mobilizon, Gancio, Friendica and Hubzilla each extend it differently. FEP-8a8e is the common-ground proposal.
  - **Address:** Mobilizon sends a `PostalAddress`; Gancio sends a plain string.
  - **Status:** Mobilizon uses iCal values (CONFIRMED, TENTATIVE, CANCELLED); Gancio has no status.
  - **New fields:** FEP-8a8e adds `previousStartTime`, `displayEndTime` and `timezone`. A missing `endTime` means "don't display an end".
  - **Mobilizon** drops an incoming `Event` that contains any property it doesn't understand.
  - **Mastodon** renders events roughly as a Note.
- **Bluesky / ATproto.** The de facto standard is `community.lexicon.calendar.event`.
  - Status enum: scheduled, rescheduled, cancelled, postponed, planned.
  - Mode enum: in-person, virtual, hybrid.
  - Time and location fields: `startsAt`, plus a typed `locations` array.
  - RSVPs live in separate `calendar.rsvp` records.
- **Mobilizon** serves a public GraphQL API at `POST {instance}/api` and exports group events as ICS. That is cleaner than federation.
- **Meetup** has a GraphQL API; network-wide queries require Meetup Pro.
- **Facebook Events** are closed: reading them needs a token from an event admin plus App Review.
- **TiBillet** has no documented event export format. Read the Lespass Django models or ask the Code Commun people.
- **OpenAgenda** stores one event with N `timings` and external IDs (`extIds`), and maps to schema.org `eventSchedule`, `startDate` and `endDate`.
- **DATAtourisme** publishes an RDF ontology (run by ADN Tourisme) with stable URIs. The simplified CSV has one row per event, with periods grouped together.

### Observed data problems
- **Events past midnight.** An event running past midnight was published with the end *before* the start (19:00 → 01:00 on the same date).
- **Occurrences vs events.** The office's agenda site counts occurrences, while DATAtourisme has one record per event.

### Open schema questions for the spec run (Pierre answers these)
1. **Primary key: the event or the occurrence?** A weekly market is 1 record in OpenAgenda and 52 in Google's model. Which do you store, and which do you derive?
2. **One merged record or per-source claims?** The claim form would be `(field, value, source, seen_at)`, with the canonical view computed from the claims. This drives dedup and provenance.
3. **Status superset.** How do you represent "postponed, no new date yet"?
4. **Time.** Local wall-clock time plus an IANA zone, or UTC? FEP-8a8e had to add `timezone` for this reason.
5. **Custom model or schema.org/DATAtourisme plus extensions?** What would a custom model buy you?
6. **Moderation state.** Does each event and claim carry a moderation status (draft, pending, published, rejected)?

---

## 4. Event sources

### 4.1 Office de tourisme (confirmed path)
- **Who.** Office de Tourisme des Baronnies en Drôme Provençale, Nyons. It shares the site dromeprovencale.fr with three other offices.
- **How events get in.** Organisers submit through the **Apidae Event** form (`event.apidae-tourisme.com`, `territoire=71846`, probably the Drôme provençale territory ID, *not verified*). The office moderates. Records live in **Apidae**, which syndicates to **DATAtourisme**, dromeprovencale.fr, Unidivers, fest.fr and probably the local press.
- **The free route is DATAtourisme on data.gouv.fr**, under Licence Ouverte / Etalab 2.0. You must cite the source and the last update date, and not distort the data.
  - **Freshness.** The export is regenerated nightly (around 03:00 UTC), so lag is about **one day**.
  - **Coverage.** For the week of 16–21 Oct 2026, **13/13** events on the office's agenda were present, including lotos, flea markets and village gatherings, not only tourist content.
  - **Query.** The tabular API works when direct downloads are blocked:
    ```sh
    curl -s 'https://tabular-api.data.gouv.fr/api/resources/2c6023f2-ffce-4c1a-9728-bdc020e463b5/data/?Code_postal_et_commune__contains=26110&page_size=50'
    ```
    Add `&page=N` to paginate.
  - **Useful columns:** `Nom_du_POI`, `Code_postal_et_commune`, `Periodes_regroupees`, `Createur_de_la_donnee`, `SIT_diffuseur`, `Date_de_mise_a_jour`.
  - **Filtering.** **Filter on `Createur_de_la_donnee` or on GPS distance, not on postcode.** The office's area spans several postcodes and even crosses into the Hautes-Alpes.
  - **No images.** The simplified CSV has none. For images and richer fields, use the full N-Triples dump (about 1.9 GB) or a free datatourisme.fr API key.
  - **Image rights.** Images have their own copyright, separate from the database licence.
- **Direct Apidae access** requires the **office's agreement** (an API project on the Apidae developer platform, with read rights granted by the contributing member). It mainly adds photos. The office will ask what you give back.
- **Pushing back upstream is possible.** Curated events could be sent to Apidae through the form or the write API. That is the commons-friendly reciprocity and the argument for a partnership with the office.

### 4.2 Other known local channels
- **Ensemble ici** (Nyons and Baronnies) is a citizen media site run by the association DECOR at the Maison des Possibles. It already ingests the office's data (probably Apidae) and receives some events by mailing list. It is a potential partner or competitor, not a source to scrape.
- **Venterol** publishes on the mairie website and on CityAll, a closed Lumiplan product with no API found.
- **Le Dauphiné and La Tribune** have no structured feed. They are probably reprints of Apidae or OpenAgenda, or editorial text with no `Event` markup.
- **Other channels:** HelloAsso, parish newsletters (PDF, low volume, older audience), local radio, flyers.
- **Most of the non-tourist local life is on Facebook groups, Instagram and WhatsApp**, which are all closed.

### 4.3 Direct publishing (humans and agents)
- There will be a human form and MCP or API write tools for agents.
- **Constraint:** everything goes through curation, with spam control.
- To specify: moderation queue, trust levels (e.g. verified organisers), rate limits, and who moderates and how many hours a week.

### 4.4 Facebook and Instagram: the opt-in tagging idea
- **Ruled out:** scraping with throwaway accounts or automation that evades detection. The reasons are Meta's terms, the GDPR, and the project's own values.
- **The idea:** organisers or users tag our account on IG or FB, and an automated pipeline turns the post into an event draft. Tagging is opt-in, so it is consent-shaped.
- **Feasibility.** *Unverified, check during the spec:*
  - The Instagram Graph API notifies a Business or Creator account through mention webhooks and lets it fetch the media that mentions it.
  - Facebook Pages have a similar mention webhook.
  - Both require a Meta app, App Review and business verification.
- **Pipeline sketch:** mention webhook → fetch the caption and image → LLM extraction (flyer OCR plus text) → draft event with provenance → moderation queue → published.
- **Open questions:**
  - What incentive do organisers and users have to tag? Without one, the pipeline stays empty.
  - Do we republish images and captions, or only extracted fields?
  - Do we need terms stating that tagging means consent to republish?
- **What Meta allows third-party apps to publish:**
  - Facebook Page posts: yes, through the Graph API.
  - Facebook Events: no.
  - Facebook Groups: no (that API was removed in 2024).
  - Instagram: Business or Creator accounts only, image required.
  - WhatsApp groups: no.

---

## 5. Publishing and interfaces

- **Web:** a list page plus **one page per occurrence** with Google-compliant JSON-LD.
- **Public API:** API-first, documented with OpenAPI.
- **MCP server.**
  - *Read tools:* search by town, travel time, date and category.
  - *Write tools:* submissions, which always land in moderation. Authentication is to be designed.
- **Feeds to consider:**
  - **ICS**, the cheapest incentive for organisers.
  - **RSS**, as agendamilitant does.
  - **ActivityPub** following FEP-8a8e.
  - Bluesky `community.lexicon.calendar.event` records.
  - **Write-back to Apidae.**
- **Licensing:**
  - Code licence: presumably AGPL, to confirm.
  - Data licence: if any source is ODbL, the merged dataset inherits share-alike.
  - Attribute Licence Ouverte sources with the source and update date.

---

## 6. Open forks for the spec run

**Product**
1. **Offline-first for whom?** A reader browsing a cached agenda needs a service worker. An organiser drafting offline needs sync and conflict resolution.
2. **Audience:** locals, tourists, or both? That decides the default weight of Apidae's tourist content.
3. **Area:** the office's territory, a fixed list of communes, or "N minutes from Nyons" with a hard cap?
4. **Language:** French only?
5. **Categories and editorial line:** two filter axes like agendamilitant? Is there a charter?
6. **bobine.art:** what to take from it?

**Feeding**

7. **Write-back to Apidae.** If yes, the data model needs external IDs.
8. **Curation:** who moderates, how many hours, trust levels, and whether agents and humans share one queue.
9. **Incentive to tag us on IG and FB.**
10. **Republishing images and captions from Meta.**

**Stack (to `/madr`)**

11. NixOS vs Debian, decided mainly by the colleague's Nix fluency.
12. Django vs PostgREST (plus a second service) vs Rust.
13. Envoy vs nginx vs Caddy.
14. Pure Nix vs docker compose. Must stay coherent with fork 11.
15. Front-end approach, which depends on fork 1.
16. Backup RPO and RTO targets, and how backups split between ZFS snapshots and database backups to object storage.
17. CI or none (are tests run on the laptop only?) and who may deploy.
18. Code and data licences.

**Schema:** questions 1–6 in section 3.

---

## 7. Notes for the spec skill
- Pierre prefers a Socratic, adversarial mode: ask, challenge, don't hand over answers.
- Pierre prefers a CLI-first, Nix-based workflow, and no `.docx`.
- Expand/contract migrations must be part of the spec from day one, not added later.
- First vertical slice to consider:
  1. DATAtourisme ingestion for the office's area;
  2. normalisation into the internal model;
  3. a list page with the town and travel-time filter;
  4. occurrence pages with JSON-LD.

  Everything else (submissions, MCP, Meta, offline) builds on that.

