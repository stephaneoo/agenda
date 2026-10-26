# Facts about the prod host, shared by its NixOS configuration and the laptop apps
# (deploy, status, restore, admin-tunnel). From `infra/tofu output` (Task 13).
rec {
  # The flexible IPv4 (nl-ams-1). No domain yet: the site is served on it.
  address = "51.15.79.107";
  # The public host name: the IP itself, with a Let's Encrypt short-lived IP certificate.
  siteHost = address;
  siteUrl = "https://${siteHost}";
  # Scaleway SBS volumes, by ID (the sda/sdb order isn't stable).
  rootDisk = "/dev/disk/by-id/scsi-0SCW_sbs_volume-a91d9256-457b-4a1c-ac65-482a104486c5";
  zfsDisk = "/dev/disk/by-id/scsi-0SCW_sbs_volume-9bc9c6ad-7e1d-4749-abca-b03db862c786";
  # pgBackRest repository (fr-par, another region than the VM).
  s3Endpoint = "s3.fr-par.scw.cloud";
  s3Region = "fr-par";
  walsBucket = "agenda-wals";
  # Who may SSH in as root: the prod key (.secrets/agenda_prod_ed25519 on Pierre's laptop,
  # used for nothing else; the dev VM's key is a different one); the colleague's goes here too.
  rootKeys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGAlTnOewh7fbQt51IqIhiXfr89EiN75wssOCMWFiGPP agenda-prod-root"
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIERzQeRCQAQBnzsYqE7y+KZANE/j8x51s2SfAl3TmHcQ agenda-claude"
  ];
}
