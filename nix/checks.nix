# Checks run by `nix flake check`. The deploy refuses to go ahead if any fails.
#
# The build sandbox has no .git, so the expand/contract migration lint, which needs the
# deploy tags, can't live here: it is the `check-migrations` app, and `nix run .#check`
# runs both.
{
  lib,
  self,
  host,
  ops,
  agendaApp,
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
    export FAKE_INDEXEDDB=${fakeIndexeddb}
    ${nodejs}/bin/node --test 'tests/js/*.test.js'
    # Paris time must not depend on the machine's zone.
    TZ=America/Los_Angeles ${nodejs}/bin/node --test 'tests/js/*.test.js'
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

  # infra/ is formatted and valid (no network, no backend), with the provider release that
  # infra/.terraform.lock.hcl pins: its zip, fetched by the lock's zh: hash, from a local
  # mirror, so `tofu init` checks it against the lock too. nixpkgs' provider is older.
  # Bump `version` and the hashes together with the lock file (infra/README.md).
  infra-validate =
    let
      version = "2.88.0";
      platform =
        {
          x86_64-linux = {
            name = "linux_amd64";
            sha256 = "1e2fe730b88b984c23f64ac72c39e6d90de0cc381d9108294b84652241992c7b";
          };
          aarch64-darwin = {
            name = "darwin_arm64";
            sha256 = "92bef9f8bec704fb3d5448194ecec9b9db50fe22714168a293adafed8faf19a2";
          };
          x86_64-darwin = {
            name = "darwin_amd64";
            sha256 = "aae56c9f4eaf94401cbe1d9db46043d62b501b99e985271c3e39aa95990e0b59";
          };
        }
        .${pkgs.stdenv.hostPlatform.system};
      zip = pkgs.fetchurl {
        url = "https://github.com/scaleway/terraform-provider-scaleway/releases/download/v${version}/terraform-provider-scaleway_${version}_${platform.name}.zip";
        inherit (platform) sha256;
      };
    in
    pkgs.runCommand "agenda-infra-validate" { nativeBuildInputs = [ pkgs.opentofu ]; } ''
      mirror=$TMPDIR/mirror/registry.opentofu.org/scaleway/scaleway
      mkdir -p $mirror
      cp ${zip} $mirror/terraform-provider-scaleway_${version}_${platform.name}.zip
      cp -r ${self}/infra infra
      chmod -R u+w infra
      cd infra
      export HOME=$TMPDIR TF_IN_AUTOMATION=1
      tofu fmt -check -diff
      tofu init -backend=false -input=false -lockfile=readonly -plugin-dir=$TMPDIR/mirror >/dev/null
      tofu validate
      touch $out
    '';

  # The host configuration evaluates (any system: evaluation only, nothing is built, so this
  # runs on a Mac too).
  host-eval = pkgs.writeText "agenda-host-eval" (
    builtins.unsafeDiscardStringContext host.config.system.build.toplevel.drvPath
  );

  # The laptop apps build (shellcheck runs on each), on every system.
  ops-apps = pkgs.linkFarmFromDrvs "agenda-ops-apps" [
    ops.status
    ops.ssh
    ops.restore
    ops.deploy
    ops.admin-tunnel
  ];

  # Every package in uv.lock comes from files.pythonhosted.org.
  lock-sources = withSource "agenda-lock-sources" ''
    python -m tools.check_lock_sources uv.lock
  '';
}
# Linux only: builds the whole system. Its closure carries no browser (the e2e's Chromium and
# Playwright), and the app source has no tools/, tests/ or docs/.
// lib.optionalAttrs (pkgs.stdenv.hostPlatform.system == host.pkgs.stdenv.hostPlatform.system) {
  host-closure =
    pkgs.runCommand "agenda-host-closure"
      {
        exportReferencesGraph = [
          "graph"
          host.config.system.build.toplevel
        ];
      }
      ''
        if grep -iE '^/nix/store/[^/]*(chromium|playwright)' graph; then
          echo "the system closure contains a browser" >&2
          exit 1
        fi
        for d in tools tests docs infra; do
          if [ -e ${agendaApp.src}/$d ]; then
            echo "the app source contains $d/" >&2
            exit 1
          fi
        done
        touch $out
      '';
}
