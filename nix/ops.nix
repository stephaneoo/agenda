# The laptop's commands against the prod host (any system, a Mac included). They only need
# ssh: everything Linux runs on the host itself.
#   nix run .#status                 the MOTD report, over SSH
#   nix run .#ssh -- '<command>'     a command on the host as root (no command: a shell)
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
    # Each argument as one word for the remote shell; nothing at all for no argument.
    quoted() { if [ "$#" -gt 0 ]; then printf '%q ' "$@"; fi; }
  '';
  mk =
    name: text:
    pkgs.writeShellApplication {
      inherit name;
      # The shared prelude sets the SSH variables; not every command uses all of them.
      excludeShellChecks = [ "SC2034" ];
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

  # Arguments cross a remote shell: each is quoted (printf %q) so it stays one word.
  status = mk "agenda-status-remote" ''
    exec ssh "''${ssh_opts[@]}" "$host" "agenda-status show $(quoted "$@")"
  '';

  # The command is one string, run by root's shell on the host (as with plain ssh).
  ssh = mk "agenda-ssh" ''
    tty=()
    if [ -t 0 ]; then tty=(-t); fi
    exec ssh "''${ssh_opts[@]}" "''${tty[@]}" "$host" "$@"
  '';

  # The restore runs on the host (the same agenda-restore the monthly test runs); this only
  # carries the arguments there. A terminal (-t) so that Ctrl-C reaches it.
  restore = mk "agenda-restore-remote" ''
    tty=()
    if [ -t 0 ]; then tty=(-t); fi
    exec ssh "''${ssh_opts[@]}" "''${tty[@]}" "$host" "agenda-restore $(quoted "$@")"
  '';

  # tools/deploy.py, from the working tree's root. nix itself comes from the user's PATH (the
  # daemon's, on a Mac).
  deploy = mk "agenda-deploy" ''
    export AGENDA_HOST=${site.address} AGENDA_KNOWN_HOSTS=${knownHosts}
    if [ -z "''${AGENDA_SSH_KEY:-}" ] && [ -f "$root/.secrets/agenda_ed25519" ]; then
      export AGENDA_SSH_KEY="$root/.secrets/agenda_ed25519"
    fi
    cd "$root"
    exec ${pkgs.python312}/bin/python3 -I ${../tools/deploy.py} "$@"
  '';

  admin-tunnel = mk "agenda-admin-tunnel" ''
    port=''${1:-8000}
    echo "Admin: http://localhost:$port/admin/  (Ctrl-C to close the tunnel)"
    exec ssh "''${ssh_opts[@]}" -N -o ExitOnForwardFailure=yes -L "$port:127.0.0.1:8000" "$host"
  '';
}
