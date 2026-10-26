# The prod host (plan, Task 14): a Scaleway DEV1-M in nl-ams-1.
{
  config,
  lib,
  pkgs,
  modulesPath,
  site,
  agendaStatus,
  ...
}:
let
  # Fixed IDs: the nftables egress rules match on them, and must load before any name lookup.
  agendaUid = 980;
  postgresUid = config.ids.uids.postgres;

  # The names each restricted user may reach on 443, resolved into nftables sets by
  # agenda-egress.service (the addresses change; the app also refuses any other host).
  egressNames = {
    agenda = [
      "tabular-api.data.gouv.fr"
      "www.data.gouv.fr"
    ];
    postgres = [
      site.s3Endpoint
      "${site.walsBucket}.${site.s3Endpoint}"
    ];
  };

  egressRefresh = pkgs.writeShellApplication {
    name = "agenda-egress-refresh";
    runtimeInputs = [
      pkgs.nftables
      pkgs.getent
      pkgs.gawk
      pkgs.coreutils
    ];
    text = ''
      # Resolve each allowed name (through systemd-resolved, like the jobs do) and replace
      # the user's set in one nft transaction. A name that doesn't resolve leaves its set
      # as it was, so a DNS hiccup never empties it.
      refresh() {
        local set=$1; shift
        local ips=()
        for name in "$@"; do
          mapfile -t found < <(getent ahostsv4 "$name" | awk '{print $1}' | sort -u)
          if [ "''${#found[@]}" -eq 0 ]; then
            echo "agenda-egress: $name does not resolve; $set kept as it was" >&2
            return 0
          fi
          ips+=("''${found[@]}")
        done
        local elements
        elements=$(printf '%s\n' "''${ips[@]}" | sort -u | paste -sd, -)
        printf 'flush set inet agenda_egress %s\nadd element inet agenda_egress %s { %s }\n' \
          "$set" "$set" "$elements" | nft -f -
        echo "agenda-egress: $set = $elements"
      }
      refresh agenda_v4 ${lib.escapeShellArgs egressNames.agenda}
      refresh postgres_v4 ${lib.escapeShellArgs egressNames.postgres}
    '';
  };
in
{
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
    ./disko.nix
    ./postgres.nix
    ./app.nix
    ./caddy.nix
    ./timers.nix
    ./backup.nix
  ];

  system.stateVersion = "26.05";
  networking.hostName = "agenda";
  # ZFS needs a stable host ID.
  networking.hostId = "a9e1da27";
  time.timeZone = "UTC";

  # --- Boot: GRUB on the ESP as the removable-media path (no reliance on NVRAM), plus BIOS.
  boot.loader.grub = {
    enable = true;
    efiSupport = true;
    efiInstallAsRemovable = true;
    configurationLimit = 20;
  };
  boot.initrd.availableKernelModules = [
    "virtio_scsi"
    "virtio_pci"
    "sd_mod"
    "sr_mod"
  ];
  boot.supportedFilesystems = [ "zfs" ];
  boot.zfs.extraPools = [ "tank" ];
  boot.zfs.forceImportRoot = false;
  # 4 GB of RAM: keep the ARC to 1 GB, PostgreSQL has its own cache.
  boot.kernelParams = [ "zfs.zfs_arc_max=1073741824" ];
  services.zfs.autoScrub = {
    enable = true;
    interval = "monthly";
  };
  zramSwap.enable = true;

  # --- Network: a routed /32 by DHCP (with classless routes); resolved for DNS.
  networking.useDHCP = false;
  networking.useNetworkd = true;
  systemd.network.networks."10-wan" = {
    matchConfig.Name = "en*";
    networkConfig.DHCP = "ipv4";
  };
  services.resolved.enable = true;

  # Inbound: TCP 22, 80 and 443 only (openssh opens 22).
  networking.nftables.enable = true;
  networking.firewall = {
    enable = true;
    allowedTCPPorts = [
      80
      443
    ];
    allowedUDPPorts = [ ];
  };

  # Outbound allow-list for the app and database users (plan, Task 14): loopback (resolved
  # at 127.0.0.53, PostgreSQL, gunicorn), replies, and 443 to the resolved sets. Everything
  # else they send is rejected. Other users (root, nix, caddy for ACME) are not restricted.
  networking.nftables.tables.agenda_egress = {
    family = "inet";
    content = ''
      set agenda_v4 { type ipv4_addr; }
      set postgres_v4 { type ipv4_addr; }

      chain output {
        type filter hook output priority filter; policy accept;
        meta skuid != { ${toString agendaUid}, ${toString postgresUid} } accept
        oif "lo" accept
        ct state established,related accept
        meta skuid ${toString agendaUid} meta nfproto ipv4 tcp dport 443 ip daddr @agenda_v4 accept
        meta skuid ${toString postgresUid} meta nfproto ipv4 tcp dport 443 ip daddr @postgres_v4 accept
        meta skuid { ${toString agendaUid}, ${toString postgresUid} } counter log prefix "agenda-egress drop: " level info limit rate 6/minute
        meta skuid { ${toString agendaUid}, ${toString postgresUid} } reject
      }
    '';
  };

  systemd.services.agenda-egress = {
    description = "Refresh the egress allow-list sets from DNS";
    after = [
      "nftables.service"
      "network-online.target"
      "systemd-resolved.service"
    ];
    wants = [ "network-online.target" ];
    # nftables.service reloads empty the sets: refill them right after.
    wantedBy = [
      "multi-user.target"
      "nftables.service"
    ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${egressRefresh}/bin/agenda-egress-refresh";
    };
  };
  systemd.timers.agenda-egress = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "1min";
      OnUnitActiveSec = "10min";
    };
  };

  # --- Access: root by key only.
  services.openssh = {
    enable = true;
    openFirewall = true;
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "prohibit-password";
      X11Forwarding = false;
    };
  };
  users.users.root.openssh.authorizedKeys.keys = site.rootKeys;
  users.mutableUsers = false;

  # One user for every app job (gunicorn, the sync, the projection rebuild, migrations).
  users.users.agenda = {
    isSystemUser = true;
    uid = agendaUid;
    group = "agenda";
    home = "/var/lib/agenda";
  };
  users.groups.agenda.gid = agendaUid;

  # --- Secrets: sops-nix, decrypted with the host's SSH key (age, through ssh-to-age).
  sops = {
    defaultSopsFile = ../../../secrets/agenda.yaml;
    age.sshKeyPaths = [ "/etc/ssh/ssh_host_ed25519_key" ];
    # Read by systemd as root (LoadCredential), never by the app user directly.
    secrets.django-secret-key = {
      mode = "0400";
      restartUnits = [ "agenda-web.service" ];
    };
  };

  # --- Nix: deploys copy closures in as root; builds run here (the laptop may be a Mac).
  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    trusted-users = [ "root" ];
  };
  nix.gc = {
    automatic = true;
    dates = "weekly";
    options = "--delete-older-than 30d";
  };

  # --- The status report: at every interactive login, and `agenda-status show`.
  environment.systemPackages = [
    agendaStatus
    pkgs.curl
    pkgs.jq
    pkgs.htop
    pkgs.pgbackrest
  ];
  environment.loginShellInit = ''
    if [[ $- == *i* ]]; then agenda-status show || true; fi
  '';
}
