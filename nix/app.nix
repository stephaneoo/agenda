# The app as the host runs it (plan, Task 14): the production venv, the source, and the
# collected static files.
{
  lib,
  pkgs,
  pythonSet,
  workspace,
  geoEnv,
  root,
}:
rec {
  # deps.default: the project's dependencies only, no pytest or linters.
  venv = pythonSet.mkVirtualEnv "agenda-host-env" workspace.deps.default;

  # The app directories only: no tools/, tests/, docs/, infra/ or nix/.
  src = lib.fileset.toSource {
    inherit root;
    fileset = lib.fileset.unions (
      map (p: root + "/${p}") [
        "agenda_site"
        "agenda"
        "api"
        "claims"
        "geo"
        "ingest"
        "web"
        "manage.py"
      ]
    );
  };

  # The committed geo data on its own store path: its hash changes only with the data, which
  # is what tells the host to run load_geo again.
  data = lib.fileset.toSource {
    inherit root;
    fileset = lib.fileset.unions [
      (root + "/data/travel-times.json")
      (root + "/data/communes.geojson")
    ];
  };

  # collectstatic at build time, on the production settings with a build-only key: Caddy
  # serves this store path, and nothing on the host writes static files.
  static =
    pkgs.runCommand "agenda-static"
      (
        geoEnv
        // {
          nativeBuildInputs = [ venv ];
          AGENDA_ENV = "production";
          DJANGO_SECRET_KEY = "build-only-collectstatic-not-a-real-key-0c9e1f7a3b5d2e8c4a6f";
          DJANGO_ALLOWED_HOSTS = "build.invalid";
        }
      )
      ''
        export HOME=$TMPDIR DJANGO_STATIC_ROOT=$out
        cd ${src}
        python manage.py collectstatic --noinput >/dev/null
      '';
}
