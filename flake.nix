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
            runtimeInputs = [ postgres ] ++ lib.optional pkgs.stdenv.isLinux pkgs.util-linux;
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
            '';
          };
          # Everything the deploy runs: the sandboxed flake checks, then the git-aware ones.
          checkAll = pkgs.writeShellApplication {
            name = "check";
            runtimeInputs = [ checkMigrations ];
            text = ''
              nix flake check -L
              check-migrations
            '';
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
            ;
        }
      );
    in
    {
      packages = forAllSystems (system: {
        venv = perSystem.${system}.venv;
        postgis-fixture = perSystem.${system}.postgisFixture;
      });

      apps = forAllSystems (system: {
        check-migrations = {
          type = "app";
          program = "${perSystem.${system}.checkMigrations}/bin/check-migrations";
        };
        check = {
          type = "app";
          program = "${perSystem.${system}.checkAll}/bin/check";
        };
      });

      checks = forAllSystems (
        system:
        import ./nix/checks.nix {
          inherit lib self;
          inherit (perSystem.${system})
            pkgs
            venv
            postgisFixture
            geoEnv
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
            ];
            env = s.geoEnv // {
              AGENDA_DEV = "1";
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
