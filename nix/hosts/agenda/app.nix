# The Django app (plan, Task 14): gunicorn behind Caddy, migrations, geo data, the sync job,
# and `agenda-manage` for one-off management commands. Every app job runs as `agenda`.
{
  config,
  lib,
  pkgs,
  site,
  agendaApp,
  agendaStatus,
  egressRefresh,
  recordIfSilent,
  ...
}:
let
  inherit (agendaApp)
    venv
    src
    data
    static
    geoEnv
    ;
  python = "${venv}/bin/python";
  manage = "${python} ${src}/manage.py";

  # The environment of every app process. No secret here: the key is a credential.
  env = geoEnv // {
    AGENDA_ENV = "production";
    AGENDA_SITE_URL = site.siteUrl;
    # The public host; localhost and 127.0.0.1 for the SSH tunnel and the deploy's check on
    # gunicorn's loopback port. Never "*".
    DJANGO_ALLOWED_HOSTS = "${site.siteHost},localhost,127.0.0.1";
    DATABASE_URL = "postgis://agenda@%2Frun%2Fpostgresql/agenda";
    DJANGO_STATIC_ROOT = "${static}";
    AGENDA_STATUS_FILE = "/var/lib/agenda/status.json";
    PYTHONDONTWRITEBYTECODE = "1";
    PYTHONUNBUFFERED = "1";
    HOME = "/var/lib/agenda";
  };

  credential = "django-secret-key:${config.sops.secrets.django-secret-key.path}";

  # The sandbox of every app process: the units below and `agenda-manage`'s transient ones.
  # None of them binds a port (gunicorn gets its sockets from systemd), none needs a
  # capability, a namespace, or any system call outside @system-service.
  sandbox = {
    NoNewPrivileges = true;
    PrivateTmp = true;
    PrivateDevices = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectKernelLogs = true;
    ProtectControlGroups = true;
    ProtectClock = true;
    ProtectProc = "invisible";
    ProcSubset = "pid";
    RestrictNamespaces = true;
    RestrictSUIDSGID = true;
    LockPersonality = true;
    CapabilityBoundingSet = "";
    RestrictAddressFamilies = [
      "AF_UNIX"
      "AF_INET"
      "AF_INET6"
    ];
    SocketBindDeny = "any";
    SystemCallArchitectures = "native";
    SystemCallFilter = "@system-service";
    SystemCallErrorNumber = "EPERM";
  };

  # As `systemd-run -p` arguments.
  sandboxProps = lib.concatStringsSep " " (
    lib.mapAttrsToList (
      k: v:
      let
        value =
          if builtins.isBool v then
            (if v then "yes" else "no")
          else if builtins.isList v then
            lib.concatStringsSep " " v
          else
            toString v;
      in
      "-p ${lib.escapeShellArg "${k}=${value}"}"
    ) sandbox
  );

  # What every app unit shares: the user, the state directory, the key, and the sandbox.
  common = sandbox // {
    User = "agenda";
    Group = "agenda";
    StateDirectory = "agenda";
    StateDirectoryMode = "0750";
    WorkingDirectory = "${src}";
    LoadCredential = [ credential ];
    UMask = "0027";
  };

  appUnit = {
    after = [
      "postgresql.service"
      "agenda-db-setup.service"
    ];
    requires = [
      "postgresql.service"
      "agenda-db-setup.service"
    ];
    # Unit files expand %-specifiers in Environment=: the URL's %2F must be written %%2F.
    environment = lib.mapAttrs (_: lib.replaceStrings [ "%" ] [ "%%" ]) env;
  };

  # load_geo replaces the communes and matrix: only when the committed data changed.
  loadGeo = pkgs.writeShellScript "agenda-load-geo" ''
    set -eu
    stamp=/var/lib/agenda/geo-loaded
    if [ "$(cat "$stamp" 2>/dev/null || true)" = "${data}" ]; then
      echo "geo data ${data} already loaded"
      exit 0
    fi
    ${manage} load_geo --matrix ${data}/data/travel-times.json --communes ${data}/data/communes.geojson
    echo "${data}" > "$stamp"
  '';

  # `agenda-manage <command> [args]`: a management command as `agenda`, with the same
  # environment and key as the units (a transient unit, so the key stays a credential).
  # `--database-url=<url>` first points it at another database (the restore's temporary one).
  agendaManage = pkgs.writeShellScriptBin "agenda-manage" ''
    db=()
    case "''${1:-}" in
      --database-url=*) db=(--setenv="DATABASE_URL=''${1#--database-url=}"); shift ;;
    esac
    if [ -t 0 ] && [ -t 1 ]; then mode=--pty; else mode=--pipe; fi
    exec ${config.systemd.package}/bin/systemd-run --quiet --wait --collect "$mode" \
      --unit="agenda-manage-$$" \
      -p User=agenda -p Group=agenda \
      -p StateDirectory=agenda -p StateDirectoryMode=0750 \
      -p WorkingDirectory=${src} \
      -p LoadCredential=${credential} \
      -p After=postgresql.service \
      -p UMask=0027 \
      ${sandboxProps} \
      ${lib.concatStringsSep " " (lib.mapAttrsToList (k: v: "--setenv=${k}=${lib.escapeShellArg v}") env)} \
      "''${db[@]}" \
      ${python} ${src}/manage.py "$@"
  '';
in
{
  environment.systemPackages = [ agendaManage ];
  _module.args.agendaManage = agendaManage;

  # Migrations (axes, ingest and the moderators group included) run before gunicorn starts,
  # at every deploy whose code changed, and before /healthz is checked.
  systemd.services.agenda-migrate = lib.recursiveUpdate appUnit {
    description = "agenda: apply migrations and load the geo data";
    wantedBy = [ "multi-user.target" ];
    restartTriggers = [
      src
      data
    ];
    serviceConfig = common // {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = [
        "${manage} migrate --noinput"
        "${loadGeo}"
      ];
    };
  };

  # gunicorn on two sockets, held by systemd so restarts drop no connection:
  # - a Unix socket only Caddy can reach (owner agenda, group caddy, 0660). Over it gunicorn
  #   sets REMOTE_ADDR to "", so nothing through Caddy is loopback; SECURE_PROXY_SSL_HEADER is
  #   trusted because only Caddy, which sets X-Forwarded-Proto itself, connects here;
  # - 127.0.0.1:8000, closed from outside by the firewall: the deploy's /healthz check and
  #   the admin through an SSH tunnel.
  systemd.sockets.agenda-web = {
    description = "agenda: gunicorn sockets";
    wantedBy = [ "sockets.target" ];
    listenStreams = [
      "/run/agenda-web/gunicorn.sock"
      "127.0.0.1:8000"
    ];
    socketConfig = {
      SocketUser = "agenda";
      SocketGroup = "caddy";
      SocketMode = "0660";
      DirectoryMode = "0755";
      Backlog = 256;
    };
  };

  systemd.services.agenda-web = lib.recursiveUpdate appUnit {
    description = "agenda: gunicorn";
    after = [ "agenda-migrate.service" ];
    requires = [
      "agenda-migrate.service"
      "agenda-web.socket"
    ];
    wantedBy = [ "multi-user.target" ];
    restartTriggers = [
      src
      venv
    ];
    serviceConfig = common // {
      Type = "notify";
      NotifyAccess = "main";
      # 3 workers for 3 vCPUs and 4 GB; a request gets 30 s, a restart 20 s to drain.
      ExecStart = lib.concatStringsSep " " [
        "${venv}/bin/gunicorn agenda_site.wsgi:application"
        "--workers 3"
        "--timeout 30"
        "--graceful-timeout 20"
        "--keep-alive 5"
        "--max-requests 2000"
        "--max-requests-jitter 200"
        "--limit-request-line 4094"
        "--limit-request-fields 50"
        "--access-logfile -"
        "--error-logfile -"
      ];
      ExecReload = "${pkgs.coreutils}/bin/kill -HUP $MAINPID";
      Restart = "on-failure";
      RestartSec = "2s";
      MemoryMax = "1500M";
    };
  };

  # The nightly DATAtourisme sync. The run has its own 15-minute deadline; these bound the
  # unit in case it hangs. systemd ignores RuntimeMaxSec on a oneshot (it says so), so the
  # one-hour bound is TimeoutStartSec.
  systemd.services.sync-datatourisme = lib.recursiveUpdate appUnit {
    description = "agenda: DATAtourisme sync";
    after = [
      "agenda-migrate.service"
      "agenda-egress.service"
      "network-online.target"
    ];
    wants = [
      "agenda-egress.service"
      "network-online.target"
    ];
    requires = [ "agenda-migrate.service" ];
    serviceConfig = common // {
      Type = "oneshot";
      # As root ("+"): the egress sets, fresh, just before the run goes out.
      ExecStartPre = "+${egressRefresh}";
      ExecStart = "${manage} sync_datatourisme";
      TimeoutStartSec = "1h";
      MemoryMax = "512M";
      ExecStopPost = recordIfSilent "sync-datatourisme" "sync-datatourisme.service";
    };
  };

  # Rebuilding the projection by hand: `agenda-manage rebuild_projection`.
}
