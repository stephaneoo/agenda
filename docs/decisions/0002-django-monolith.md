---
status: accepted
date: 2026-10-25
decision-makers: Pierre
---

# A single Django codebase for the app server

## Context and Problem Statement

The app server has several jobs: an automatic admin area, a public API with OpenAPI, scheduled ingestion jobs (DATAtourisme first), an MCP server, and server-rendered pages with JSON-LD, one per occurrence. Which stack does all of that?

## Considered Options

* Django (GeoDjango, Django admin, django-ninja, management commands)
* PostgREST over an `api` schema of views, plus a second service for ingestion, MCP and pages, with react-admin
* Rust

## Decision Outcome

Chosen option: "Django", to keep it simple. One language, one codebase, one deployable unit, and the admin area comes ready-made.

Rust was dropped because it has no automatic admin. PostgREST was rejected because it needs a second service, puts much of the logic in SQL, and needs an admin front end we would maintain ourselves.

### Consequences

* Good, because admin, API, ingestion, pages and MCP share the same models and tests.
* Bad, because the API is defined in code and not by the schema. API-first becomes a discipline: the OpenAPI spec is generated and checked into the repo, and the UI uses no private endpoint.
* Bad, because Django migrations aren't zero-downtime by default. Expand/contract has to be enforced by tooling and review.

### Confirmation

* A check fails when a migration is backward-incompatible with the code currently deployed (dropped or renamed column, NOT NULL without default, and the like) and isn't marked as the contract step of an earlier expand.
* A check fails when the generated OpenAPI spec differs from the one committed.

## Hard constraints

* Every breaking schema change is split over two deploys: expand, then contract.
* The public API is documented by a generated OpenAPI spec committed to the repo.
* The MCP server calls the same service layer as the API. It has no separate data access.
