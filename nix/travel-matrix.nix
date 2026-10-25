# `nix run .#travel-matrix -- --origin 26220 --minutes 30`: regenerates data/travel-times.json
# and data/communes.geojson from OSM (Geofabrik) and IGN ADMIN EXPRESS COG with OSRM.
# Needs about 8 GB of RAM and 5 GB of disk in ~/.cache/agenda-travel-matrix.
{
  lib,
  pkgs,
  venv,
  geoEnv,
}:
pkgs.writeShellApplication {
  name = "travel-matrix";
  runtimeInputs = [
    venv
    pkgs.git
    pkgs.curl
    pkgs.osmium-tool
    pkgs.osrm-backend
    pkgs.gdalMinimal
    pkgs.p7zip
  ];
  text = ''
    ${lib.concatStrings (lib.mapAttrsToList (k: v: "export ${k}=${v}\n") geoEnv)}
    export OSRM_CAR_PROFILE=${pkgs.osrm-backend}/share/osrm/profiles/car.lua
    cd "$(git rev-parse --show-toplevel)"
    exec python -m tools.travel_matrix "$@"
  '';
}
