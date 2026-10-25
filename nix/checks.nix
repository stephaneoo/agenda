# Checks run by `nix flake check`. The deploy refuses to go ahead if any fails.
#
# The build sandbox has no .git, so the expand/contract migration lint, which needs the
# deploy tags, can't live here: it is the `check-migrations` app, and `nix run .#check`
# runs both.
{
  lib,
  self,
  pkgs,
  venv,
  postgisFixture,
  geoEnv,
  nodejs,
  fakeIndexeddb,
}:
let
  # Runs a script from a writable copy of the repo.
  withSource =
    name: script:
    pkgs.runCommand name
      (
        geoEnv
        // {
          nativeBuildInputs = [
            venv
            pkgs.git
          ];
          AGENDA_DEV = "1";
        }
      )
      ''
        cp -r ${self} src
        chmod -R u+w src
        cd src
        export HOME=$TMPDIR
        ${script}
        touch $out
      '';
in
{
  pytest = withSource "agenda-pytest" ''
    ${postgisFixture}/bin/postgis-fixture pytest -p no:cacheprovider
  '';

  # The offline filter and store (web/static/web), on node's test runner.
  js = withSource "agenda-js" ''
    FAKE_INDEXEDDB=${fakeIndexeddb} ${nodejs}/bin/node --test 'tests/js/*.test.js'
  '';

  openapi = withSource "agenda-openapi" ''
    python -m tools.check_openapi
  '';

  # Django's deployment checklist on the production settings. The key and host are dummies
  # that exist only in this check; the settings refuse AGENDA_DEV in production.
  deploy-settings = withSource "agenda-deploy-settings" ''
    env -u AGENDA_DEV AGENDA_ENV=production \
      DJANGO_SECRET_KEY=check-only-not-a-real-key-6f1c9b2e8d4a7f3c5e0b9a1d2c8e7f4a \
      DJANGO_ALLOWED_HOSTS=agenda.example.org \
      python manage.py check --deploy --fail-level WARNING
  '';

  # Every package in uv.lock comes from files.pythonhosted.org.
  lock-sources = withSource "agenda-lock-sources" ''
    python -m tools.check_lock_sources uv.lock
  '';
}
