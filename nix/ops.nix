# The laptop's commands against the prod host (any system, a Mac included). They only need
# ssh: everything Linux runs on the host itself.
#   nix run .#status                 the MOTD report, over SSH
#   nix run .#admin-tunnel           the admin on http://localhost:8000/admin/ (Ctrl-C closes it)
#   nix run .#restore -- [args]      tools/restore.py on the host (Task 15)
#   nix run .#deploy                 tools/deploy.py (Task 16)
# SSH: root@<address>, the host key pinned by nix/hosts/agenda/known_hosts. The key is
# $AGENDA_SSH_KEY, else .secrets/agenda_ed25519 in the repo if present, else ssh's default.
{
  lib,
  pkgs,
  site,
}:
let
  knownHosts = ../nix/hosts/agenda/known_hosts;
  sshPrelude = ''
    root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
    ssh_opts=(-o "UserKnownHostsFile=${knownHosts}" -o StrictHostKeyChecking=yes -o ConnectTimeout=30)
    if [ -n "''${AGENDA_SSH_KEY:-}" ]; then
      ssh_opts+=(-i "$AGENDA_SSH_KEY")
    elif [ -f "$root/.secrets/agenda_ed25519" ]; then
      ssh_opts+=(-i "$root/.secrets/agenda_ed25519")
    fi
    host=root@${site.address}
  '';
  mk =
    name: text:
    pkgs.writeShellApplication {
      inherit name;
      # On a Mac, Apple's ssh (/usr/bin): nixpkgs' would refuse Apple-only options such as
      # UseKeychain in ~/.ssh/config, and the system's agent and keychain belong to it.
      runtimeInputs = lib.optional (!pkgs.stdenv.isDarwin) pkgs.openssh ++ [
        pkgs.git
        pkgs.coreutils
      ];
      text = sshPrelude + text;
    };
in
{
  inherit knownHosts sshPrelude;

  status = mk "agenda-status-remote" ''
    exec ssh "''${ssh_opts[@]}" "$host" agenda-status show "$@"
  '';

  admin-tunnel = mk "agenda-admin-tunnel" ''
    port=''${1:-8000}
    echo "Admin: http://localhost:$port/admin/  (Ctrl-C to close the tunnel)"
    exec ssh "''${ssh_opts[@]}" -N -o ExitOnForwardFailure=yes -L "$port:127.0.0.1:8000" "$host"
  '';
}
