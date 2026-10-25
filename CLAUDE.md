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
- `python -m tools.check_openapi --write`: regenerate `api/openapi.json` after changing the API
  (and bump `api.v1.VERSION`). The flake check fails while the committed file differs.
- `python manage.py rebuild_projection`: rebuild the canonical `Event`/`Occurrence` tables from
  the claims. Write claims only through `claims.services.write_claims` / `delete_claim`; the
  canonical models refuse writes outside `agenda.models.projection_writer()`.
- `uv lock` (inside the dev shell): update `uv.lock` after editing `pyproject.toml`. uv only
  resolves releases at least 7 days old (`exclude-newer` in `pyproject.toml`).

On the Mac laptop without Nix, run commands on the dev VM: `tools/dev run '<command>'`, and bring
generated files back with `tools/dev pull <path>`. The sync sends what `git ls-files` lists
(tracked, plus untracked but not ignored) and deletes on the VM what is gone locally. Flakes only see files tracked by git, so
`git add` new files before running Nix.

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
- `GDAL_LIBRARY_PATH`, `GEOS_LIBRARY_PATH`: set by the dev shell to the Nix copies.

## Secrets

Never commit or copy `.env`, `.secrets/`, keys or passwords.
