# Build status, agenda v1

Kept up to date by the orchestrator (Claude). The newest entry is at the bottom.

## Setup (2026-10-25)
- Pierre approved the cost estimate in chat.
- Scaleway has no DEV1 quota in fr-par on this account. VMs therefore go in **nl-ams-1** and buckets in **fr-par**, still in different regions. Spec and plan are updated.
- Dev VM `agenda-dev` (DEV1-L, nl-ams-1), with its server ID in `.secrets/dev-server-id`. Nix is installed there and KVM is available. **It is deleted at the end of the build session.**
- `tools/dev run '<cmd>'` runs commands on the dev VM. Secrets never leave the laptop, except the SCW_* variables, which reach the VM only for the duration of a single `run-scw` command.

## Batches
Each batch goes through: implement, review, implementer fixes, security review, fresh fixer, orchestrator verdict.

| Batch | Tasks | State |
|---|---|---|
| A | 1–4: flake, checks, geo, travel matrix | **shipped**: c6e1978..fca2e01 |
| B | 5–9: claims, projection, read layer, API, web | in progress |
| C | 10–12: ingestion, admin, offline | pending |
| D | 13–17: infra, host, backups, deploy, runbook | pending |

## Batch A verdict (2026-10-25)
- Review: 3 majors (migration check: contract-of too permissive, unnumbered files skipped, per-operation tests missing) and 10 minors. All fixed.
- Security: 4 mediums (prod HTTPS settings, dev-mode guard, deny-list sync, old rsync for pull) and lows. All fixed, or noted in the plan for Tasks 11, 13 and 14.
- Orchestrator verification: `nix run .#check` all green, 99 tests, 0 migration violations, no secret-like file tracked.
- 64 communes covered within 30 min of Nyons. The data has version `2026-10-25 osm:2026-10-24 ign:2026-01-01`.

## Open risks
- **Darwin is unverified.** The flake's darwin outputs evaluate but have never been built on a Mac. The colleague deploys from a Mac, so `nix run .#check` and the deploy have to be built and run on a Mac before handover.
