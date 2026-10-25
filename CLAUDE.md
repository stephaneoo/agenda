# Agenda

Local events agenda around Nyons. Django monolith packaged with uv2nix, deployed to a NixOS host.

- Spec: `docs/specs/2026-10-25-agenda-v1.md`
- Plan: `docs/plans/2026-10-25-agenda-v1.md` (follow its Global Constraints)
- Decisions: `docs/decisions/`

## Commands

- `nix develop`: dev shell with Python, Django, pytest, PostgreSQL + PostGIS and uv.
- `nix develop -c pytest [path]`: run tests. `pytest` in the shell starts an ephemeral PostGIS
  (`nix/postgis-fixture.sh`) unless `DATABASE_URL` is already set.
- `nix flake check -L`: every check the deploy runs (pytest, and the checks in `nix/checks.nix`).
- `uv lock` (inside the dev shell): update `uv.lock` after editing `pyproject.toml`.

On the Mac laptop without Nix, run commands on the dev VM: `tools/dev run '<command>'`, and bring
generated files back with `tools/dev pull <path>`. Flakes only see files tracked by git, so
`git add` new files before running Nix.

## Environment

- `DATABASE_URL`: `postgis://user@%2Fpath%2Fto%2Fsocket/dbname` (socket dir percent-encoded in the host).
- `DJANGO_SECRET_KEY`: required outside the dev shell.
- `AGENDA_DEV=1`: dev shell and checks only. Enables DEBUG and an insecure dev key.
- `GDAL_LIBRARY_PATH`, `GEOS_LIBRARY_PATH`: set by the dev shell to the Nix copies.

## Secrets

Never commit or copy `.env`, `.secrets/`, keys or passwords.
