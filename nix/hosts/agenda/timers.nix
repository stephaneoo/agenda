# systemd timers (plan, Task 14; the backup ones are in backup.nix):
# - sync-datatourisme: daily at 06:00 Europe/Paris, after the upstream export (~03:00 UTC);
# - ZFS snapshots of tank/pg: hourly, 48 kept, for local rollback.
{ lib, ... }:
{
  # Only datasets with com.sun:auto-snapshot=true (tank/pg, disko.nix).
  services.zfs.autoSnapshot = {
    enable = true;
    flags = "-k -p --utc";
    frequent = 0;
    hourly = 48;
    daily = 0;
    weekly = 0;
    monthly = 0;
  };

  systemd.timers = {
    sync-datatourisme = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "*-*-* 06:00 Europe/Paris";
        # A run missed while the host was down happens at boot.
        Persistent = true;
        RandomizedDelaySec = "2min";
      };
    };
  }
  # The module defines a timer per interval; with nothing to keep, they would only create a
  # snapshot and destroy it at once.
  // lib.genAttrs [
    "zfs-snapshot-frequent"
    "zfs-snapshot-daily"
    "zfs-snapshot-weekly"
    "zfs-snapshot-monthly"
  ] (_: { enable = false; });
}
