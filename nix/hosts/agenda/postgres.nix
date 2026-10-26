# PostgreSQL 16 + PostGIS, its data on tank/pg (mounted by ZFS at /var/lib/postgresql), WAL
# archived to pgBackRest (backup.nix).
{
  config,
  lib,
  pkgs,
  ...
}:
let
  pg = config.services.postgresql.finalPackage;
in
{
  services.postgresql = {
    enable = true;
    package = pkgs.postgresql_16;
    extensions = ps: [ ps.postgis ];
    dataDir = "/var/lib/postgresql/16";
    enableTCPIP = false;
    ensureDatabases = [ "agenda" ];
    ensureUsers = [
      {
        name = "agenda";
        ensureDBOwnership = true;
      }
    ];
    # Unix socket only, peer authentication: the OS user is the role.
    authentication = lib.mkForce ''
      local all all peer
    '';
    settings = {
      # Unix socket only (/run/postgresql). enableTCPIP = false still listens on localhost:
      # nothing uses TCP (the app, pgBackRest, the restore's temporary instance, the status
      # report all go through sockets), so no port at all.
      listen_addresses = lib.mkForce "";
      # Sized for 4 GB of RAM, shared with the ARC (1 GB) and gunicorn.
      shared_buffers = "512MB";
      effective_cache_size = "1536MB";
      work_mem = "16MB";
      maintenance_work_mem = "128MB";
      max_connections = 50;
      wal_level = "replica";
      wal_compression = "on";
      max_wal_size = "1GB";
      archive_mode = "on";
      archive_command = "${pkgs.pgbackrest}/bin/pgbackrest --stanza=agenda archive-push %p";
      # RPO 5 minutes (spec): a WAL segment is switched and archived at least that often.
      archive_timeout = 300;
      log_min_duration_statement = 1000;
      log_line_prefix = "%m [%p] %q%u@%d ";
    };
  };

  systemd.services.postgresql = {
    # Never start on the root disk if the pool isn't mounted: it would initdb an empty
    # database there.
    after = [ "zfs-mount.service" ];
    requires = [ "zfs-mount.service" ];
    unitConfig.AssertPathIsMountPoint = "/var/lib/postgresql";
  };

  # PostGIS needs a superuser to be created; the app's role owns the database only.
  systemd.services.agenda-db-setup = {
    description = "Create the PostGIS extension in the agenda database";
    after = [
      "postgresql.service"
      "postgresql-setup.service"
    ];
    requires = [ "postgresql.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      User = "postgres";
      ExecStart = "${pg}/bin/psql -v ON_ERROR_STOP=1 -d agenda -c 'CREATE EXTENSION IF NOT EXISTS postgis'";
    };
  };
}
