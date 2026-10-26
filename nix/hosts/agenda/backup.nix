# pgBackRest: WAL archive and base backups in agenda-wals (fr-par, offsite), encrypted
# client-side (the bucket is plain object storage; the ZFS pool itself is unencrypted, spec).
# Not the NixOS pgbackrest module: its jobs can't carry the tier annotations.
{
  config,
  lib,
  pkgs,
  site,
  ...
}:
let
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

  systemd.tmpfiles.rules = [
    "d /var/spool/pgbackrest 0750 postgres postgres -"
    "d /var/log/pgbackrest 0750 postgres postgres -"
  ];

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
