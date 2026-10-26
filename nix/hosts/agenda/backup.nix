# pgBackRest: WAL archive and base backups in agenda-wals (fr-par, offsite), encrypted
# client-side (the bucket is plain object storage; the ZFS pool itself is unencrypted, spec).
# Not the NixOS pgbackrest module: its jobs can't carry the tier annotations.
{
  config,
  lib,
  pkgs,
  site,
  agendaStatus,
  agendaManage,
  egressRefresh,
  ...
}:
let
  pg = config.services.postgresql.finalPackage;
  python = "${pkgs.python312}/bin/python3";
  tz = "PYTHONTZPATH=${pkgs.tzdata}/share/zoneinfo";

  # tools/backup_tiers.py and tools/restore.py, as root commands on the host. (They are
  # operations scripts, packaged on their own: the app's source excludes tools/.)
  agendaBackup = pkgs.writeShellApplication {
    name = "agenda-backup";
    runtimeInputs = [
      pkgs.pgbackrest
      pkgs.util-linux
      agendaStatus
    ];
    text = ''
      export ${tz}
      exec ${python} -I ${../../../tools/backup_tiers.py} "$@"
    '';
  };
  agendaRestore = pkgs.writeShellApplication {
    name = "agenda-restore";
    runtimeInputs = [
      pkgs.pgbackrest
      pkgs.util-linux
      pkgs.coreutils
      pkgs.gnugrep
      pkgs.curl
      pkgs.bash
      config.boot.zfs.package
      config.systemd.package
      pg
      agendaStatus
      agendaManage
      agendaBackup
    ];
    text = ''
      export ${tz}
      export PATH="${dirOf egressRefresh}:$PATH"
      exec ${python} -I ${../../../tools/restore.py} "$@"
    '';
  };

  # Timer jobs as root (they drop to postgres and agenda themselves), after the network and
  # the egress sets.
  job = description: exec: {
    inherit description;
    after = [
      "postgresql.service"
      "pgbackrest-stanza.service"
      "agenda-egress.service"
      "network-online.target"
    ];
    wants = [
      "agenda-egress.service"
      "network-online.target"
    ];
    requires = [ "postgresql.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStartPre = egressRefresh;
      ExecStart = exec;
      TimeoutStartSec = "3h";
    };
  };
  timer = calendar: {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = calendar;
      Persistent = true;
      RandomizedDelaySec = "5min";
    };
  };
  pgbackrestConf = pkgs.writeText "pgbackrest.conf" ''
    [global]
    repo1-type=s3
    repo1-s3-endpoint=${site.s3Endpoint}
    repo1-s3-region=${site.s3Region}
    repo1-s3-bucket=${site.walsBucket}
    repo1-s3-uri-style=path
    repo1-path=/pgbackrest
    repo1-cipher-type=aes-256-cbc
    # Expiry by age and tier is delegated to agenda-backup expire (tools/backup_tiers.py):
    # pgBackRest itself never expires a full backup by count.
    repo1-retention-full-type=count
    repo1-retention-full=9999
    # WAL for point-in-time recovery over the last 7 days, with one full backup a day: the
    # newest 8 full backups keep their WAL (backup_tiers.ARCHIVE_RETENTION_FULL); older
    # backups keep only what makes them consistent.
    repo1-retention-archive-type=full
    repo1-retention-archive=8
    archive-async=y
    spool-path=/var/spool/pgbackrest
    compress-type=zst
    process-max=2
    start-fast=y
    log-level-console=info
    log-level-file=detail
    log-path=/var/log/pgbackrest

    [global:archive-push]
    process-max=2

    [agenda]
    pg1-path=${config.services.postgresql.dataDir}
    pg1-socket-path=/run/postgresql
  '';
in
{
  environment.etc."pgbackrest/pgbackrest.conf".source = pgbackrestConf;

  # The keys (Tofu output, scoped to agenda-wals) and the repository passphrase, from sops,
  # in an include file only postgres reads.
  sops.secrets = {
    pgbackrest-s3-key = { };
    pgbackrest-s3-key-secret = { };
    pgbackrest-cipher-pass = { };
  };
  sops.templates."pgbackrest-secrets.conf" = {
    owner = "postgres";
    mode = "0400";
    path = "/etc/pgbackrest/conf.d/secrets.conf";
    content = ''
      [global]
      repo1-s3-key=${config.sops.placeholder.pgbackrest-s3-key}
      repo1-s3-key-secret=${config.sops.placeholder.pgbackrest-s3-key-secret}
      repo1-cipher-pass=${config.sops.placeholder.pgbackrest-cipher-pass}
    '';
  };

  # archive_command runs inside the postgresql unit, which NixOS sandboxes (ProtectSystem=
  # strict): the async archiver needs its spool and log directories writable there.
  systemd.services.postgresql.serviceConfig.ReadWritePaths = [
    "/var/spool/pgbackrest"
    "/var/log/pgbackrest"
  ];

  systemd.tmpfiles.rules = [
    "d /var/spool/pgbackrest 0750 postgres postgres -"
    "d /var/log/pgbackrest 0750 postgres postgres -"
  ];

  environment.systemPackages = [
    agendaBackup
    agendaRestore
  ];

  # Every night a full backup with its tier, then the expiry by tier; the monthly restore
  # test runs steps 1-2 of the restore (tools/restore.py --test-only) on the latest backup.
  # Each records its outcome in the status file.
  systemd.services.pgbackrest-backup = job "pgBackRest: full backup, annotated by tier" "${agendaBackup}/bin/agenda-backup run";
  systemd.timers.pgbackrest-backup = timer "*-*-* 01:30 Europe/Paris";
  systemd.services.pgbackrest-tier-expire =
    job "pgBackRest: expire backups past their tier's age" "${agendaBackup}/bin/agenda-backup expire";
  systemd.timers.pgbackrest-tier-expire = timer "*-*-* 02:30 Europe/Paris";
  systemd.services.restore-test = job "Restore test: the latest backup into tank/pg-restore, sanity checks" "${agendaRestore}/bin/agenda-restore --test-only";
  systemd.timers.restore-test = timer "*-*-02 04:00 Europe/Paris";

  # The stanza, once the database runs (idempotent: a no-op when it exists and matches).
  systemd.services.pgbackrest-stanza = {
    description = "pgBackRest: create the agenda stanza";
    after = [
      "postgresql.service"
      "agenda-egress.service"
      "network-online.target"
    ];
    wants = [
      "agenda-egress.service"
      "network-online.target"
    ];
    requires = [ "postgresql.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      User = "postgres";
      ExecStart = "${pkgs.pgbackrest}/bin/pgbackrest --stanza=agenda stanza-create";
      Restart = "on-failure";
      RestartSec = "1min";
    };
  };
}
