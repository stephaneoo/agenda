{
  description = "Agenda: local events around Nyons";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
        "x86_64-linux"
      ];
      forAllSystems = lib.genAttrs systems;

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

      perSystem = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet =
            (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python312; }).overrideScope
              (
                lib.composeManyExtensions [
                  pyproject-build-systems.overlays.wheel
                  overlay
                ]
              );
          venv = pythonSet.mkVirtualEnv "agenda-env" workspace.deps.all;
          postgres = pkgs.postgresql_16.withPackages (p: [ p.postgis ]);
          postgisFixture = pkgs.writeShellApplication {
            name = "postgis-fixture";
            runtimeInputs = [
              postgres
              pkgs.coreutils
            ]
            ++ lib.optional pkgs.stdenv.isLinux pkgs.util-linux;
            text = builtins.readFile ./nix/postgis-fixture.sh;
          };
          so = pkgs.stdenv.hostPlatform.extensions.sharedLibrary;
          # GeoDjango loads GDAL and GEOS with ctypes; point it at the Nix copies.
          # gdalMinimal: the full gdal pulls arrow-cpp, broken on x86_64-darwin.
          geoEnv = {
            GDAL_LIBRARY_PATH = "${pkgs.gdalMinimal}/lib/libgdal${so}";
            GEOS_LIBRARY_PATH = "${pkgs.geos}/lib/libgeos_c${so}";
          };
          # Runs pytest on a fresh PostGIS unless DATABASE_URL is already set.
          pytestShim = pkgs.writeShellScriptBin "pytest" ''
            if [ -z "''${DATABASE_URL:-}" ]; then
              exec ${postgisFixture}/bin/postgis-fixture ${venv}/bin/pytest "$@"
            fi
            exec ${venv}/bin/pytest "$@"
          '';
          checkMigrations = pkgs.writeShellApplication {
            name = "check-migrations";
            runtimeInputs = [
              venv
              pkgs.git
              postgisFixture
            ];
            text = ''
              ${lib.concatStrings (lib.mapAttrsToList (k: v: "export ${k}=${v}\n") geoEnv)}
              # Settings only, for rendering SQL: no secret key or host config needed.
              export AGENDA_DEV=1
              cd "$(git rev-parse --show-toplevel)"
              postgis-fixture python -m tools.check_migrations "$@"
              # The API spec changed since the last deploy only with a new info.version.
              python -m tools.check_openapi_version "$@"
            '';
          };
          # Offline reading end to end, in headless Chromium (Linux only: nixpkgs' Playwright
          # browsers). Starts its own PostGIS and app; needs no network.
          e2ePython = pkgs.python312.withPackages (p: [ p.playwright ]);
          e2e = pkgs.writeShellApplication {
            name = "e2e";
            runtimeInputs = [
              venv
              postgisFixture
              e2ePython
              pkgs.git
              pkgs.coreutils
            ];
            text = ''
              ${lib.concatStrings (lib.mapAttrsToList (k: v: "export ${k}=${v}\n") geoEnv)}
              export AGENDA_DEV=1
              export PLAYWRIGHT_BROWSERS_PATH=${pkgs.playwright-driver.browsers}
              export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true
              # Chromium aborts without a font configuration.
              export FONTCONFIG_FILE=${pkgs.makeFontsConf { fontDirectories = [ pkgs.dejavu_fonts ]; }}
              status_dir=$(mktemp -d)
              trap 'rm -rf "$status_dir"' EXIT
              export AGENDA_STATUS_FILE="$status_dir/status.json"
              cd "$(git rev-parse --show-toplevel)"
              postgis-fixture ${e2ePython}/bin/python -I tools/e2e_offline.py ${venv}/bin/python
            '';
          };
          # Everything the deploy runs: the sandboxed flake checks, then the git-aware ones,
          # then (on Linux) the browser check.
          checkAll = pkgs.writeShellApplication {
            name = "check";
            runtimeInputs = [ checkMigrations ];
            text = ''
              nix flake check -L
              check-migrations
              ${lib.optionalString pkgs.stdenv.isLinux "${e2e}/bin/e2e"}
            '';
          };
          travelMatrix = import ./nix/travel-matrix.nix { inherit lib pkgs venv geoEnv; };
          # JS unit tests (tests/js) run on node's own test runner; IndexedDB is faked with
          # fake-indexeddb, pinned here (Apache-2.0, no dependencies).
          nodejs = pkgs.nodejs;
          fakeIndexeddb = pkgs.fetchzip {
            url = "https://registry.npmjs.org/fake-indexeddb/-/fake-indexeddb-6.2.5.tgz";
            hash = "sha256-r3vi0MU+nBY9fPr2rW8Cw2Z1YkGAkQhf4x0wVhlC6+8=";
          };
        in
        {
          inherit
            pkgs
            venv
            postgres
            postgisFixture
            geoEnv
            pytestShim
            checkMigrations
            checkAll
            e2e
            travelMatrix
            nodejs
            fakeIndexeddb
            ;
        }
      );
    in
    {
      packages = forAllSystems (system: {
        venv = perSystem.${system}.venv;
        postgis-fixture = perSystem.${system}.postgisFixture;
      });

      apps = forAllSystems (
        system:
        lib.optionalAttrs nixpkgs.legacyPackages.${system}.stdenv.isLinux {
          e2e = {
            type = "app";
            program = "${perSystem.${system}.e2e}/bin/e2e";
          };
        }
        // {
        check-migrations = {
          type = "app";
          program = "${perSystem.${system}.checkMigrations}/bin/check-migrations";
        };
        check = {
          type = "app";
          program = "${perSystem.${system}.checkAll}/bin/check";
        };
        travel-matrix = {
          type = "app";
          program = "${perSystem.${system}.travelMatrix}/bin/travel-matrix";
        };
        }
      );

      checks = forAllSystems (
        system:
        import ./nix/checks.nix {
          inherit lib self;
          inherit (perSystem.${system})
            pkgs
            venv
            postgisFixture
            geoEnv
            nodejs
            fakeIndexeddb
            ;
        }
      );

      devShells = forAllSystems (
        system:
        let
          s = perSystem.${system};
        in
        {
          default = s.pkgs.mkShell {
            # The shim comes first so that `pytest` gets a database.
            packages = [
              s.pytestShim
              s.venv
              s.postgisFixture
              s.postgres
              s.pkgs.uv
              s.nodejs
              # Operations: infra/ (OpenTofu, scw), secrets/ (sops, age).
              s.pkgs.opentofu
              s.pkgs.scaleway-cli
              s.pkgs.sops
              s.pkgs.age
              s.pkgs.ssh-to-age
              s.pkgs.jq
            ];
            env = s.geoEnv // {
              AGENDA_DEV = "1";
              FAKE_INDEXEDDB = "${s.fakeIndexeddb}";
              UV_NO_SYNC = "1";
              UV_PYTHON = "${s.venv}/bin/python";
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              unset PYTHONPATH
            '';
          };
        }
      );

      formatter = forAllSystems (system: perSystem.${system}.pkgs.nixfmt-rfc-style);
    };
}
