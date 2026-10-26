# Root disk: GPT, BIOS boot + ESP + ext4 (Scaleway boots UEFI; the BIOS partition keeps a
# legacy boot possible). The block volume: the ZFS pool `tank`, unencrypted (spec).
#
# The datasets get ZFS-managed mountpoints (options.mountpoint, no disko `mountpoint`, so no
# fstab entry): the restore swaps datasets by renaming them (tools/restore.py), which an
# fstab entry naming the dataset would not survive.
{ site, ... }:
{
  disko.devices = {
    disk.root = {
      type = "disk";
      device = site.rootDisk;
      content = {
        type = "gpt";
        partitions = {
          bios = {
            size = "1M";
            type = "EF02";
          };
          esp = {
            size = "512M";
            type = "EF00";
            content = {
              type = "filesystem";
              format = "vfat";
              mountpoint = "/boot";
              mountOptions = [ "umask=0077" ];
            };
          };
          root = {
            size = "100%";
            content = {
              type = "filesystem";
              format = "ext4";
              mountpoint = "/";
            };
          };
        };
      };
    };

    disk.zfs = {
      type = "disk";
      device = site.zfsDisk;
      content = {
        type = "zfs";
        pool = "tank";
      };
    };

    zpool.tank = {
      type = "zpool";
      options.ashift = "12";
      rootFsOptions = {
        mountpoint = "none";
        canmount = "off";
        compression = "lz4";
        atime = "off";
        xattr = "sa";
        acltype = "posixacl";
        # Only tank/pg is snapshotted hourly (timers.nix).
        "com.sun:auto-snapshot" = "false";
      };
      datasets = {
        pg = {
          type = "zfs_fs";
          options = {
            mountpoint = "/var/lib/postgresql";
            recordsize = "8K";
            logbias = "latency";
            "com.sun:auto-snapshot" = "true";
          };
        };
        # Scratch space for restores and the monthly restore test.
        pg-restore = {
          type = "zfs_fs";
          options = {
            mountpoint = "/var/lib/pg-restore";
            recordsize = "8K";
          };
        };
      };
    };
  };
}
