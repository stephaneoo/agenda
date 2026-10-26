**At the start of every session, ask Pierre whether he wants the server status (sync,
backups, restore test, disk). If he says yes, run `nix run .#status` and summarise it.**

# Agenda

Local events agenda around Nyons. Django monolith packaged with uv2nix, deployed to a NixOS host.

- Spec: `docs/specs/2026-10-25-agenda-v1.md`
- Plan: `docs/plans/2026-10-25-agenda-v1.md` (follow its Global Constraints)
- Decisions: `docs/decisions/`

## Commands

- `nix develop`: dev shell with Python, Django, pytest, PostgreSQL + PostGIS and uv.
- `nix develop -c pytest [path]`: run tests. `pytest` in the shell starts an ephemeral PostGIS
  (`nix/postgis-fixture.sh`) unless `DATABASE_URL` is already set.
- `nix run .#e2e` (Linux only): offline reading end to end in headless Chromium (Playwright from
  nixpkgs): seeds an ephemeral PostGIS (`tools/e2e_seed.py`), runs the app on 127.0.0.1:8765, goes
  offline, checks the banner, the filters against the shared cases, cached and IndexedDB-rendered
  occurrence pages, and that nothing violates the CSP. `nix run .#check` runs it last on Linux; on
  a Mac it is skipped (run it on the dev VM).
- `nix run .#check`: every check the deploy runs: `nix flake check -L` (pytest, OpenAPI diff,
  `manage.py check --deploy` on production settings, uv.lock sources limited to PyPI, see
  `nix/checks.nix`), then `nix run .#check-migrations`, the expand/contract lint against the latest
  `deploy-*` tag. That one needs git history, which the flake check sandbox doesn't have.
- Backward-incompatible migration operations must carry `# contract-of: <app>.<expand_migration>`,
  naming a migration already present at the last deploy.
- `nix run .#travel-matrix -- --origin 26220 --minutes 30`: regenerate `data/travel-times.json`
  and `data/communes.geojson` (OSRM on Geofabrik extracts, IGN ADMIN EXPRESS COG, La Poste
  postcodes). Needs ~8 GB RAM; caches downloads in `~/.cache/agenda-travel-matrix`. Then
  `python manage.py load_geo --matrix data/travel-times.json --communes data/communes.geojson`.
- `python -m tools.check_openapi --write`: regenerate `api/openapi.json` after changing the API,
  and bump `api.v1.VERSION`. The flake check fails while the committed file differs, and
  `nix run .#check` fails when the spec changed since the last `deploy-*` tag without a new
  `info.version` (`tools/check_openapi_version.py`).
- `python manage.py rebuild_projection`: rebuild the canonical `Event`/`Occurrence` tables from
  the claims, one event per transaction. An event that fails is logged and skipped; the command
  then exits non-zero and names the failed keys. Run it after **any change to the `Source`
  table** (priority, label, licence, external): those don't reproject by themselves.
- `python manage.py sync_datatourisme`: the nightly DATAtourisme sync (`ingest/sync.py`). Exits 1
  when a page couldn't be fetched or the run passed its deadline (`AGENDA_SYNC_DEADLINE_SECONDS`,
  default 900; no withdrawals then). Records its outcome in the status file
  (job `sync-datatourisme`), in `Source` and in `ingest.SyncRun`. A run that would withdraw more
  than 20% of the area's future occurrences is `partial` and withdraws nothing
  (`--allow-mass-withdrawal=N`, N being the "withdrawals skipped" of the last partial run,
  applies them for one run once upstream is checked, and only if the run withdraws exactly N
  with rows fetched); rows refused by `write_claims` or the database, and rows over 400 dates
  (`too_many_dates`), also make it `partial`. Every in-area row is rewritten every run (unchanged
  values are no-ops), so a withdrawn date that comes back is listed again; the payload hash
  only counts unchanged vs updated. One run at a time (advisory lock, two-int4 form, apart from the
  per-event bigint locks). Bump `ingest.sync.PARSER_VERSION` when parse or mapping change what
  a row becomes.
- Upstream hardening (`ingest/datatourisme/client.py`): https only, to `tabular-api.data.gouv.fr`
  and `www.data.gouv.fr`, no redirects, bodies streamed and capped at 20 MiB decoded,
  Retry-After capped at 60 s, every row sanitised (no NUL, no lone surrogate). The parser keeps
  dates within [run date − 1 year, run date + 2 years] (weekly ranges cut, a continuous range
  kept whole: its start is its URL) and strips C1, bidi and invisible format characters;
  `source_url` only for `https://data.datatourisme.fr/…`.
- **Never wrap many `write_claims` calls in one outer transaction** (e.g. a whole sync run):
  each call takes its event's lock until the transaction ends. One event, one transaction.
- Accepted v1 edge case: the cutoff for a `listed: false` claim (a drop after the occurrence
  ended doesn't withdraw it) comes from the occurrence's *current* `dates`. Moving its time
  later on the same day can turn an earlier drop into a withdrawal.
- `node --test 'tests/js/*.test.js'` (dev shell): the offline filter and store tests, also a flake
  check. `tests/fixtures/filter-cases.json` is shared with `tests/agenda/test_read.py`: a change to
  the search rules changes both `agenda/read.py` and `web/static/web/filter.js`.
- `uv lock` (inside the dev shell): update `uv.lock` after editing `pyproject.toml`. uv only
  resolves releases at least 7 days old (`exclude-newer` in `pyproject.toml`).

On the Mac laptop without Nix, run commands on the dev VM: `tools/dev run '<command>'`, and bring
generated files back with `tools/dev pull <path>`. The sync sends what `git ls-files` lists
(tracked, plus untracked but not ignored) and deletes on the VM what is gone locally. Flakes only see files tracked by git, so
`git add` new files before running Nix. The dev VM (`agenda-dev`) is the permanent dev and
deploy machine, not a temporary one. `tools/dev run-agent '<cmd>'` forwards a throwaway agent
holding only prod's root key (`.secrets/agenda_prod_ed25519`) for commands that SSH on to prod
(the deploy); the VM's own key (`.secrets/agenda_ed25519`) is not accepted by prod.

## Operations

The site runs on one NixOS host: `https://51.15.79.107/` (Scaleway DEV1-M, nl-ams-1; backups in
fr-par). **`docs/ops/RUNBOOK.md` is the procedure for every operation**: follow it, command by
command, and explain to the colleague in plain French what happened.

- `nix run .#status`: the status report over SSH (the same as the MOTD), jobs plus the
  host's checks (disk, pool, WAL archiving, certificate).
- `nix run .#ssh -- '<command>'`: a command on the host as root, with the pinned host key and
  `AGENDA_SSH_KEY`. Use it for every command on the server; never plain `ssh`.
- `nix run .#deploy`: the guarded deploy (`tools/deploy.py`): clean tree and `main` =
  `origin/main`, every check, the system built on the host, deploy-rs with magic rollback,
  `/healthz` (`"debug": false`) or a switch back, then the `deploy-<UTC>` tag pushed. One at
  a time, never during a restore (both flocks on the host, `deploy.HOST_LOCKS` =
  `restore.HOST_LOCKS`, taken before the e2e). From a Mac the e2e runs on the host: root
  builds it (the nix-daemon serves root only), an ephemeral user runs it in its own network
  namespace, bounded (`deploy.remote_e2e_command`); `--remote-e2e` forces that path from Linux.
- `nix run .#restore -- [--target-time T | --set L] [--test-only]`: `agenda-restore` on the
  host (`tools/restore.py`); always `--test-only` first.
- `nix run .#admin-tunnel`: the admin on `http://localhost:8000/admin/` (never public).
- On the host: `agenda-manage <command>` (a management command with the app's environment),
  `agenda-status`, `agenda-backup run|expire`, `agenda-restore`.

Safe without Pierre: deploy, rollback (system profile, see the runbook), re-running the sync, reading logs, the restore script,
unlocking a moderator. **Waits for Pierre**: changing the backup configuration or retention,
deleting backups, buckets, volumes or datasets (`tank/pg-prev-*` included), any other change
to the ZFS pool, manual SQL, `infra/` (`tofu apply`), secrets, who may SSH in.

Host layout (`nix/hosts/agenda/`): `site.nix` (address, disks, root keys), `configuration.nix`
(boot, network, firewall 22/80/443, the egress allow-list, users, sops), `postgres.nix`,
`app.nix` (gunicorn on a Unix socket for Caddy and on 127.0.0.1:8000; migrations; the sync),
`caddy.nix`, `timers.nix`, `backup.nix` (pgBackRest, tiers, restore test). `nix/app.nix`: the
production venv (`deps.default`), the source (app directories only) and the static files,
collected at build time. `nix/ops.nix`: the laptop apps.

- Egress: the `agenda` and `postgres` users may only reach DNS (through resolved on loopback)
  and 443 on the addresses their names resolved to in the last 7 days (`agenda-egress`, every
  minute and before each job): data.gouv.fr for the sync, the S3 endpoint for pgBackRest. A
  new outbound host for the app means adding its name to `egressNames`.
- Unit `Environment=` expands `%`: `app.nix` writes it `%%` (the socket URL's `%2F`).
- The status file format lives in `agenda_site/statusfile.py` (no Django), shared by the app,
  `agenda-status` and the MOTD.

## Conventions

- Write claims only through `claims.services.write_claims` / `delete_claim` (the admin and
  moderation: `moderation_write` / `moderation_delete`, source `moderation` only). `Claim` and
  `ClaimHistory` refuse writes outside `claims.models.claims_writer()`, the canonical models
  outside `agenda.models.projection_writer()`.
- **Never use raw SQL (`cursor.execute`) or `QuerySet._raw_delete()` on the canonical or claims
  tables.** The write guards can't see them. Accepted for v1; reads through the ORM are fine.
- `write_claims` and `delete_claim` raise `ValueError` for any bad input, database and date
  overflow errors included (chained). That is the only exception a caller catches per event.
- `agenda.fields.problem()` must stay total over the projection: any value it accepts, the
  projection can store. Change both together, and keep `tests/agenda/test_fields_total.py`
  generating the new cases. Bounds: years 1912–2200, INSEE communes, `seen_at` at most 5 min
  in the future, strings capped (name 300, description 10,000, URLs 2,000, others 300), values
  nested at most 32 levels.
- Log untrusted values (event keys, source data) with `%r`, never `%s`.
- The admin (`agenda/admin.py`) is read-only on `Event`/`Occurrence`; moderators act on each
  object's "Modérer" page, which calls `agenda.moderation` (hide and restore both take a reason).
  Saving a `Source` there reprojects everything after the admin's transaction commits.
- Accepted for v1: saving a `Source` in the admin runs `rebuild_all()` synchronously in that
  request (seconds for the area's few hundred events). If it grows, move it to a job.
- Accepted for v1: a moved occurrence's target is moderation-owned (its `listed: true` is a
  moderation claim). If the source later drops the whole event, the target stays shown: hide it
  by hand.
- `/admin/` answers only loopback `REMOTE_ADDR` (127.0.0.1, ::1: the SSH tunnel), dev mode
  included; anything else gets a 404 (`agenda_site.site.LoopbackAdminMiddleware`, the same
  `LOOPBACK` as `/healthz`). The test client is 127.0.0.1.
- Admin logins are throttled by django-axes (5 failures per username and address, 1 h). Unlock:
  `python manage.py axes_reset_username <name>`. Sessions last 8 h.

## Environment

- `DATABASE_URL`: `postgis://user@%2Fpath%2Fto%2Fsocket/dbname` (socket dir percent-encoded in the host).
- Secret key, required outside the dev shell (50+ characters, 5+ distinct), first found of:
  `$CREDENTIALS_DIRECTORY/django-secret-key` (systemd `LoadCredential`), the file named by
  `DJANGO_SECRET_KEY_FILE`, `DJANGO_SECRET_KEY`.
- `DJANGO_ALLOWED_HOSTS`: comma-separated. Outside dev mode each also becomes an
  `https://<host>` CSRF trusted origin, and cookies are HTTPS-only.
- `AGENDA_DEV=1`: dev shell and checks only. Enables DEBUG and an insecure dev key. Refused
  when `AGENDA_ENV=production` (set on the host) or under systemd (`INVOCATION_ID`).
- `AGENDA_SITE_URL`: root of the absolute `url` in API responses (e.g. `https://agenda.example.org`).
  Empty: the request's own scheme and host. Its origin is also the only one the service worker
  registers on (`<meta name="agenda-origin">`; unset: the request's origin in dev, none outside).
- `AGENDA_SW_KILL=1`: the service worker kill switch. `/sw.js` then clears every cache of the
  site and unregisters itself (browsers pick it up on their next visit), and pages unregister
  instead of registering. Unset it, and redeploy, to bring offline reading back. In production
  (`AGENDA_ENV=production`) `/sw.js` is a 404 to loopback, so the tunnel registers no worker.
- `AGENDA_STATUS_FILE`: the status file (`agenda_site.status.record`), default
  `/var/lib/agenda/status.json`. Set it to a scratch path when running jobs in the dev shell.
  Its directory is created 0750; `record()` logs and writes nothing when the directory is group-
  or world-writable or another user's, and the lock is never followed through a symlink.
- `AGENDA_SYNC_DEADLINE_SECONDS`: wall-clock budget of one sync run, default 900.
- `GDAL_LIBRARY_PATH`, `GEOS_LIBRARY_PATH`: set by the dev shell to the Nix copies.

## Secrets

Never commit or copy `.env`, `.secrets/`, keys or passwords. Host secrets live in
`secrets/agenda.yaml`, sops-encrypted to the host and the operator age key (`.sops.yaml`); only
the encrypted file is committed. See the runbook's "Secrets" section for where each one lives.
