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

  openapi = withSource "agenda-openapi" ''
    python -m tools.check_openapi
  '';
}
