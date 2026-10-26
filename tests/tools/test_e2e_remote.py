"""The e2e away from a working tree (batch D review, B1): from a Mac the deploy runs it on the
host, from the flake source in the host's store, where `git rev-parse` has nothing to say."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from tools import deploy
from tools.e2e_offline import resolve_root

REPO = Path(__file__).resolve().parents[2]


def test_the_root_comes_from_agenda_e2e_root() -> None:
    assert resolve_root({"AGENDA_E2E_ROOT": str(REPO)}) == REPO


@pytest.mark.parametrize("env", [{}, {"AGENDA_E2E_ROOT": ""}, {"AGENDA_E2E_ROOT": "/nonexistent"}])
def test_no_root_or_no_fixture_is_refused(env, tmp_path) -> None:
    with pytest.raises(SystemExit, match="AGENDA_E2E_ROOT"):
        resolve_root(env)
    with pytest.raises(SystemExit, match="filter-cases.json"):
        resolve_root({"AGENDA_E2E_ROOT": str(tmp_path)})


def _props(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]


def _two_commands(path: str) -> tuple[list[str], list[str]]:
    """The build (inside $(...)) and the run, split out of the one command string."""
    cmd = deploy.remote_e2e_command(path)
    build = cmd[cmd.index("$(") + 2:cmd.index(") && case")]
    run = cmd[cmd.rindex("&& systemd-run") + 3:]
    return shlex.split(build), shlex.split(run)


def test_the_remote_command_is_bounded_and_names_its_root() -> None:
    path = "/var/lib/agenda-e2e/run.abc123"
    build, run = _two_commands(path)
    # Root evaluates and builds (the nix-daemon serves root only), in a bounded scope.
    assert build[:2] == ["systemd-run", "--scope"]
    assert {"MemoryMax=1200M", "MemorySwapMax=0", "CPUWeight=20"} <= set(_props(build))
    assert build[-5:] == ["nix", "build", "--no-link", "--print-out-paths", f"path:{path}#e2e"]
    # The run: a transient service, waited for, its output piped back, with the same bounds.
    assert run[:3] == ["systemd-run", "--wait", "--pipe"] and "--scope" not in run
    assert {"MemoryMax=1200M", "MemorySwapMax=0", "CPUWeight=20"} <= set(_props(run))
    assert f"AGENDA_E2E_ROOT={path}" in run and run[-1] == "$out/bin/e2e"
    assert "git" not in deploy.remote_e2e_command(path) and not path.startswith("/tmp")


def test_the_run_is_unprivileged_and_offline() -> None:
    """Not root: an ephemeral user. Not on the host's network: its own namespace, only lo, so
    nothing else can take its port first or reach its database."""
    _, run = _two_commands("/var/lib/agenda-e2e/run.x")
    props = set(_props(run))
    assert {"DynamicUser=yes", "PrivateNetwork=yes", "NoNewPrivileges=yes", "PrivateTmp=yes"} <= props
    assert not any(p.startswith(("User=", "Group=")) for p in props)
    assert "WorkingDirectory=/var/lib/agenda-e2e/run.x" in props and any(p.startswith("RuntimeMaxSec=") for p in props)
    assert "nix" not in run  # the nix-daemon is root's only: the run never calls it


def test_only_the_e2e_run_drops_invocation_id() -> None:
    """The settings refuse AGENDA_DEV under systemd (INVOCATION_ID, which every unit gets).
    The e2e is a dev-mode harness, on its own throwaway database: its run alone drops the
    variable, nothing else does. The app units keep it, and AGENDA_ENV=production as well."""
    _, run = _two_commands("/var/lib/agenda-e2e/run.x")
    i = run.index("env")
    assert run[i:i + 3] == ["env", "-u", "INVOCATION_ID"] and "AGENDA_ENV=production" not in run
    app = (REPO / "nix/hosts/agenda/app.nix").read_text()
    assert "INVOCATION_ID" not in app and 'AGENDA_ENV = "production"' in app


def test_the_upload_and_cleanup_commands() -> None:
    upload = deploy.upload_e2e_tree_command()
    assert "mktemp -d /var/lib/agenda-e2e/run.XXXXXX" in upload and "tar -x --no-same-owner" in upload
    assert "umask 022" in upload  # readable by the e2e's user, writable by root only
    assert deploy.cleanup_e2e_command("/var/lib/agenda-e2e/run.abc123") == "rm -rf /var/lib/agenda-e2e/run.abc123"
    with pytest.raises(ValueError):
        deploy.cleanup_e2e_command("/")


def _recorder(tmp_path: Path, build_out: str) -> tuple[dict, Path]:
    """systemd-run replaced by a recorder: each call is one line of the log, its words split
    by \\x1f. The scope's call (the build) prints `build_out`, as nix would print a store path.
    Shell built-ins only: the build sandbox has no coreutils on /usr/bin:/bin."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemd-run").write_text(
        '#!/bin/sh\n'
        'for w in "${0##*/}" "$@"; do printf "%s\\037" "$w"; done >> "$LOG"; echo >> "$LOG"\n'
        f'case " $* " in *" --scope "*) printf "%s\\n" {shlex.quote(build_out)};; esac\n')
    (bin_dir / "systemd-run").chmod(0o755)
    log = tmp_path / "log"
    log.write_text("")
    return {"PATH": f"{bin_dir}:/usr/bin:/bin", "LOG": str(log)}, log


def _calls(log: Path) -> list[list[str]]:
    return [line.split("\x1f")[:-1] for line in log.read_text().splitlines()]


def test_the_remote_command_runs_as_written(tmp_path) -> None:
    """The real string, through a shell, with systemd-run replaced by a recorder: the words it
    receives are the ones the host would."""
    env, log = _recorder(tmp_path, "/nix/store/abc-e2e")
    path = "/var/lib/agenda-e2e/run.abc 123"
    subprocess.run(["sh", "-c", deploy.remote_e2e_command(path)], check=True, env=env)
    build, run = _calls(log)
    assert build[:3] == ["systemd-run", "--scope", "--quiet"] and build[-1] == f"path:{path}#e2e"
    assert run[:3] == ["systemd-run", "--wait", "--pipe"] and f"AGENDA_E2E_ROOT={path}" in run
    assert f"WorkingDirectory={path}" in run and run[-1] == "/nix/store/abc-e2e/bin/e2e"


def test_the_remote_command_refuses_without_a_build(tmp_path) -> None:
    env, log = _recorder(tmp_path, "error: something")
    res = subprocess.run(["sh", "-c", deploy.remote_e2e_command("/var/lib/agenda-e2e/run.x")], env=env,
                         capture_output=True, text=True)
    assert res.returncode != 0 and len(_calls(log)) == 1


def test_the_flake_e2e_wrapper_takes_its_root_from_the_environment() -> None:
    flake = (REPO / "flake.nix").read_text()
    wrapper = flake[flake.index("e2e = pkgs.writeShellApplication"):flake.index("checkAll =")]
    assert "git rev-parse" not in wrapper and "AGENDA_E2E_ROOT" in wrapper


linux_proc = pytest.mark.skipif(not Path("/proc/net/tcp").exists(), reason="needs Linux /proc")


@linux_proc
def test_the_e2e_knows_its_own_server_from_another_listener() -> None:
    """wait_for_server only trusts /healthz when the listener on the port is its own child's
    socket: something else that took the port first is refused."""
    import socket
    import sys

    from tools.e2e_offline import owns_port

    child = subprocess.Popen([sys.executable, "-c",
                              "import socket, sys, time\n"
                              "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
                              "print(s.getsockname()[1], flush=True); time.sleep(30)"],
                             stdout=subprocess.PIPE, text=True)
    other = socket.socket()
    try:
        assert child.stdout is not None
        port = int(child.stdout.readline())
        other.bind(("127.0.0.1", 0))
        other.listen()
        other_port = other.getsockname()[1]
        assert owns_port(child.pid, port)
        assert not owns_port(child.pid, other_port)  # a listener that isn't the child's
        assert not owns_port(child.pid, 1)  # nobody listens
    finally:
        other.close()
        child.kill()
        child.wait()
