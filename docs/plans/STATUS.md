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
| D | 13–17: infra, host, backups, deploy, runbook | **shipped**: 4c8e600..322b3b0, live tag deploy-20261026T150641Z |

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

## Batch D verdict (2026-10-26)
- **Review.** One blocker: a deploy from a Mac always refused (the e2e did `cd ""` over ssh). Fixed: the e2e takes its root explicitly, and the tree goes over as a `git archive`. Majors fixed:
  - a real restore left `archive_mode=off`;
  - S3 egress drops (the allow-set now remembers 7 days; Scaleway publishes no ranges);
  - the status said "all jobs ok" with a full disk, a degraded pool, failing archiving or an expiring cert;
  - no alert on cert renewal;
  - runbook ssh commands unusable (new `nix run .#ssh`).

  Plus 9 minors (DST tiering, PITR by time, restore and deploy locks, quoting…).
- **Security.** No high. Mediums fixed:
  - backups wipeable from the host (`agenda-wals` versioned, 30-day noncurrent expiry, the key can't delete versions);
  - nix-daemon open to all users (`allowed-users = root`);
  - the remote e2e as root with an unsandboxed Chromium (DynamicUser, PrivateNetwork, Chromium sandbox on, port ownership check).

  Lows fixed:
  - prod has its own SSH key, and the old one is refused;
  - a Scaleway security group (22/80/443, ICMP);
  - no Postgres TCP listener;
  - systemd sandboxing on the app units;
  - deploy and restore share their locks.

  Documented as accepted: the egress side channels, project-wide IAM, and that a restore trusts the backup's `postgresql.auto.conf`.
- **Orchestrator check:**
  - `origin/main` = 322b3b0. The live tag is at 203c22c; only the runbook differs since then.
  - Pages, API and healthz give 200; `/admin/` 404 from outside.
  - 5432, 8000, 2019 and 8765 are closed from outside.
  - The old key gets "Permission denied (publickey)"; the new key logs in.
  - `agenda-status show --check` passes, 0 failed units, 0 Postgres TCP listeners.
  - No secret tracked. Last full check: 695 pytest, e2e 13/13.
- **Not done:** whether the pgbackrest key can suspend versioning or change the lifecycle wasn't probed live (blocked as too close to the live bucket). The bucket policy grants neither.

## Dev VM on demand (2026-10-26)
- **Own project.** The dev VM moved to a new Scaleway project `agenda-dev` (4b0f1944-…), in OpenTofu (`infra/dev/`, state `agenda/dev.tfstate`, apart from prod's). New VM `agenda-dev` 0eedeb3b-8eb5-4ac0-8678-a3140cdc37eb: **DEV1-M** (down from DEV1-L), 40 GB SBS root, flexible IPv4 51.158.179.210, security group 22/ICMP, Determinate Nix 3.24.0 from cloud-init. Its host key was pinned against the fingerprints Scaleway's agent publishes in the server's user data (read over the API), not on first use (`tools/dev pin` does it again after a rebuild).
- **DEV1-M is enough.** `nix run .#check` passed there: cold store 17 min 36 s, warm 6 min 14 s; 695 pytest, e2e 13/13, 0 migration violations; lowest free memory 1.2 GB of 4 GB. `run-agent` reaches prod (`agenda-status show`). Not deployed.
- **Powered off between sessions.** `tools/dev up/down/state` with the key `agenda-dev-ops` (`.secrets/scw-dev.env`: list, read, start, stop the servers of `agenda-dev` only; expires 2027-10-26). Power-on to SSH 27 s, power-off 16 s. Idle €6.72/month (volume €3.80, IP €2.92) plus €0.0202 per hour up. Snapshot-and-delete would save ≈ €2.40/month but needs a key that can create servers: not done.
- **Deleted:** the old dev VM ef527daa-4a49-46dc-94a2-fb65f51f26a2 (project `agenda`), its 60 GB volume 45b66e3a-a744-468a-91f5-46ed59c8ef00 and its dynamic IP 51.15.84.31.
- **Waits for Pierre:** the laptop's Little Snitch has not yet allowed `ssh`/`rsync` to 51.158.179.210 (connections time out; the verification went through the old VM as a jump host, before its deletion). Allow it once (the address is stable), then `tools/dev up` works.
- `nix run nixpkgs#…` (the flakehub registry) times out from the new VM; the repo's flake only uses GitHub inputs, so nothing depends on it.

## Open risks
- **Darwin is unverified.** The flake's darwin outputs evaluate but have never been built on a Mac. Deploys run from the dev VM (the laptop has no Nix), and the Mac code path (`--remote-e2e`) is exercised from there.
- **The dev VM's reach from the laptop** depends on Little Snitch allowing its address (above).
