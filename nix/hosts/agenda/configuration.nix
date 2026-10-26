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
      pkgs.gnugrep
      pkgs.coreutils
      config.systemd.package
    ];
    text = ''
      # The names resolve to addresses that change every few minutes (the S3 endpoint showed
      # 85 addresses in four Scaleway /24s within 4 hours). Scaleway publishes no Object
      # Storage IP ranges (checked 2026-10-26, scaleway.com/en/docs/object-storage), and
      # pinning pgBackRest to one address would break at the next rotation. So each set holds
      # every address seen for its names in the last 7 days: the cached answer (what the
      # jobs get from resolved) and an uncached one, every minute and right before each job.
      # An address not seen yet fails for a minute at most, and the status report flags WAL
      # archiving that falls behind (agenda_site/statusfile.py). Replaced in one nft
      # transaction; a name that doesn't resolve leaves what was seen before.
      state=/var/lib/agenda-egress
      now=$(date +%s)
      ipv4='^([0-9]{1,3}\.){3}[0-9]{1,3}$'
      refresh() {
        local set=$1; shift
        local f=$state/$set
        touch "$f"
        for name in "$@"; do
          {
            getent ahostsv4 "$name" | awk '{print $1}' || true
            resolvectl query -4 --cache=no --legend=no "$name" 2>/dev/null | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' || true
          } | grep -E "$ipv4" | sort -u | awk -v t="$now" '{print $1, t}' >> "$f"
        done
        awk -v cutoff=$((now - 7 * 86400)) '
          $2 > seen[$1] { seen[$1] = $2 }
          END { for (ip in seen) if (seen[ip] >= cutoff) print ip, seen[ip] }' "$f" | sort > "$f.new"
        mv "$f.new" "$f"
        local elements
        elements=$(awk '{print $1}' "$f" | grep -E "$ipv4" | paste -sd, -)
        if [ -z "$elements" ]; then
          echo "agenda-egress: no address known for $set" >&2
          return 0
        fi
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
  # No name resolution on the local link: nothing here needs it, and LLMNR/mDNS answer or
  # trust whoever else is on the segment.
  services.resolved.settings.Resolve = {
    LLMNR = "no";
    MulticastDNS = "no";
  };

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
      StateDirectory = "agenda-egress";
      StateDirectoryMode = "0700";
    };
  };
  systemd.timers.agenda-egress = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "30s";
      OnUnitActiveSec = "1min";
      AccuracySec = "10s";
    };
  };
  _module.args.egressRefresh = "${egressRefresh}/bin/agenda-egress-refresh";

  # ExecStopPost for every timer job: when the unit failed (killed, timed out, crashed)
  # without the job recording anything since it started, record that, so the status shows
  # it instead of going STALE a day later.
  _module.args.recordIfSilent =
    job: unit:
    pkgs.writeShellScript "record-${job}-if-silent" ''
      [ "$SERVICE_RESULT" = success ] && exit 0
      started=$(${config.systemd.package}/bin/systemctl show -P InactiveExitTimestamp ${unit})
      since=$(${pkgs.coreutils}/bin/date -d "$started" +%s 2>/dev/null || echo 0)
      as_agenda=()
      [ "$(${pkgs.coreutils}/bin/id -u)" = 0 ] && as_agenda=(${pkgs.util-linux}/bin/runuser -u agenda --)
      exec "''${as_agenda[@]}" ${agendaStatus}/bin/agenda-status record-if-silent --since "$since" ${job} failed \
        "unit stopped: $SERVICE_RESULT ($EXIT_CODE $EXIT_STATUS), the job recorded nothing"
    '';

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
    # The daemon talks to root only: no other user (agenda, postgres, caddy, the e2e's
    # ephemeral one) can build, fetch or add to the store.
    allowed-users = [ "root" ];
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
