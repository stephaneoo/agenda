# Agenda

Local events agenda around Nyons. Django monolith packaged with uv2nix, deployed to a NixOS host.

- Spec: `docs/specs/2026-10-25-agenda-v1.md`
- Plan: `docs/plans/2026-10-25-agenda-v1.md` (follow its Global Constraints)
- Decisions: `docs/decisions/`

## Commands

- `nix develop`: dev shell with Python, Django, pytest, PostgreSQL + PostGIS and uv.
- `nix develop -c pytest [path]`: run tests. `pytest` in the shell starts an ephemeral PostGIS
  (`nix/postgis-fixture.sh`) unless `DATABASE_URL` is already set.
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
  when a page couldn't be fetched (no withdrawals then). Records its outcome in the status file
  (job `sync-datatourisme`), in `Source` and in `ingest.SyncRun`. A run that would withdraw more
  than 20% of the area's future occurrences is `partial` and withdraws nothing. Bump
  `ingest.sync.PARSER_VERSION` when parse or mapping change what a row becomes: unchanged
  payloads are skipped by hash.
- **Never wrap many `write_claims` calls in one outer transaction** (e.g. a whole sync run):
  each call takes its event's lock until the transaction ends. One event, one transaction.
- Accepted v1 edge case: the cutoff for a `listed: false` claim (a drop after the occurrence
  ended doesn't withdraw it) comes from the occurrence's *current* `dates`. Moving its time
  later on the same day can turn an earlier drop into a withdrawal.
- `uv lock` (inside the dev shell): update `uv.lock` after editing `pyproject.toml`. uv only
  resolves releases at least 7 days old (`exclude-newer` in `pyproject.toml`).

On the Mac laptop without Nix, run commands on the dev VM: `tools/dev run '<command>'`, and bring
generated files back with `tools/dev pull <path>`. The sync sends what `git ls-files` lists
(tracked, plus untracked but not ignored) and deletes on the VM what is gone locally. Flakes only see files tracked by git, so
`git add` new files before running Nix.

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
  in the future, strings capped (name 300, description 10,000, URLs 2,000, others 300).
- Log untrusted values (event keys, source data) with `%r`, never `%s`.

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
  Empty: the request's own scheme and host.
- `AGENDA_STATUS_FILE`: the status file (`agenda_site.status.record`), default
  `/var/lib/agenda/status.json`. Set it to a scratch path when running jobs in the dev shell.
- `GDAL_LIBRARY_PATH`, `GEOS_LIBRARY_PATH`: set by the dev shell to the Nix copies.

## Secrets

Never commit or copy `.env`, `.secrets/`, keys or passwords.
