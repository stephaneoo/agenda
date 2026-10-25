# Checks run by `nix flake check`. The deploy refuses to go ahead if any fails.
{
  lib,
  self,
  pkgs,
  venv,
  postgisFixture,
  geoEnv,
}:
let
  # A writable copy of the repo for tools that want to write next to the sources.
  withSource = name: script:
    pkgs.runCommand name
      (
        geoEnv
        // {
          nativeBuildInputs = [ venv ];
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
}
