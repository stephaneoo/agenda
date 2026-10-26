# Build status, agenda v1

Kept up to date by the orchestrator (Claude). The newest entry is at the bottom.

## Setup (2026-10-25)
- Pierre approved the cost estimate in chat.
- Scaleway has no DEV1 quota in fr-par on this account. VMs therefore go in **nl-ams-1** and buckets in **fr-par**, still in different regions. Spec and plan are updated.
- Dev VM `agenda-dev` (DEV1-L, nl-ams-1), with its server ID in `.secrets/dev-server-id`. Nix is installed there and KVM is available. ~~It is deleted at the end of the build session.~~ **2026-10-26: Pierre keeps it as the permanent development and deploy machine** (the laptop has no Nix). Prod's root SSH now has its own key (`.secrets/agenda_prod_ed25519`); the VM's key no longer opens prod.
- `tools/dev run '<cmd>'` runs commands on the dev VM. Secrets never leave the laptop, except the SCW_* variables, which reach the VM only for the duration of a single `run-scw` command.

## Batches
Each batch goes through: implement, review, implementer fixes, security review, fresh fixer, orchestrator verdict.

| Batch | Tasks | State |
|---|---|---|
| A | 1–4: flake, checks, geo, travel matrix | **shipped**: c6e1978..fca2e01 |
| B | 5–9: claims, projection, read layer, API, web | **shipped**: 0df1847..7b42767 |
| C | 10–12: ingestion, admin, offline | **shipped**: 16f9f07..ed942d1 |
| D | 13–17: infra, host, backups, deploy, runbook | implemented and live (4c8e600..11bb3bf, tag deploy-20261026T094007Z); in review |

## Batch A verdict (2026-10-25)
- Review: 3 majors (migration check: contract-of too permissive, unnumbered files skipped, per-operation tests missing) and 10 minors. All fixed.
- Security: 4 mediums (prod HTTPS settings, dev-mode guard, deny-list sync, old rsync for pull) and lows. All fixed, or noted in the plan for Tasks 11, 13 and 14.
- Orchestrator verification: `nix run .#check` all green, 99 tests, 0 migration violations, no secret-like file tracked.
- 64 communes covered within 30 min of Nyons. The data has version `2026-10-25 osm:2026-10-24 ign:2026-01-01`.

## Batch B verdict (2026-10-25)
- Review: 6 majors, all reproduced and all fixed:
  - stored XSS through `javascript:` URLs;
  - moved chains and cycles;
  - invalid winning claims, and visibility failing open;
  - the rebuild run as one giant transaction;
  - `LIKE` falling back to a seq scan outside C collation;
  - error responses that weren't problem+json.

  Plus 12 minors, all fixed.
- Security: one high, fixed: validation accepted values the projection couldn't store, which bricked restores and rebuilds. Mediums fixed: future `seen_at`, the unguarded `Claim` table, missing length caps. Lows fixed: NUL bytes giving 500s, CSP, `/healthz` details for loopback only, one generic 410 body, `safe_url` refusing userinfo, CORS, log escaping. Admin hardening (axes, IP allow-list or SSH tunnel), the Caddy rate limit and gunicorn limits are plan notes for Tasks 11 and 14.
- Orchestrator verification: `nix run .#check` all green, 410 tests, 0 migration violations, no secret tracked. API v1.2.0.
- Manual Google Rich Results Test: **not done**, because there is no browser here. It's left for Pierre once prod is up.

## Batch C verdict (2026-10-25)
- **Review.** One blocker: the unchanged-hash skip made withdrawals permanent. Fixed: every row is written on every run. Majors:
  - pagination shift (guarded with `last_modified` and a re-fetch of page 1);
  - "En cours" swamping the list (weekday expansion from names, and a collapsed group placed after the days, *Claude's call*);
  - double move (refused, and moves are atomic);
  - no browser test. `nix run .#e2e` now drives headless Chromium offline.

  Minors fixed: run lock, the "Modérateurs" group, honest offline banners, `--allow-mass-withdrawal`.
- **Security.** One high: a single NUL or lone surrogate in a row stopped every sync. Fixed by sanitising rows on arrival and isolating errors per row. Mediums:
  - streamed size cap and run deadline;
  - redirects refused, https to the two data.gouv.fr hosts only;
  - expansion window and the 400-occurrence cap;
  - offline takedown (the cache is pruned against the snapshot, plus a 30-day max age and a service-worker kill switch);
  - the admin answers only to loopback, enforced in Django.
- **Orchestrator check:** `nix run .#check` exit 0, 579 pytest, 41 JS tests (2 timezones), e2e 13 of 13 with no CSP violation, 0 migration violations, nothing secret tracked.
- **Live data:** 222 events in 35 communes. Weekday expansion applies to 11 weekly markets. `PARSER_VERSION` is 3.

## Open risks
- **Darwin is unverified.** The flake's darwin outputs evaluate but have never been built on a Mac. The colleague deploys from a Mac, so `nix run .#check` and the deploy have to be built and run on a Mac before handover.
