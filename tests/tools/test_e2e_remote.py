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


def test_the_remote_command_is_bounded_and_names_its_root() -> None:
    path = "/var/lib/agenda-e2e/run.abc123"
    cmd = deploy.remote_e2e_command(path)
    argv = shlex.split(cmd)
    # A scope: the caller's own process in a bounded cgroup, its output straight through.
    assert argv[:2] == ["systemd-run", "--scope"]
    props = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert {"MemoryMax=1200M", "MemorySwapMax=0", "CPUWeight=20"} <= set(props)
    assert f"AGENDA_E2E_ROOT={path}" in argv
    assert argv[-3:] == ["nix", "run", f"path:{path}#e2e"]
    assert "git" not in cmd and "cd " not in cmd and not path.startswith("/tmp")


def test_only_the_e2e_scope_drops_invocation_id() -> None:
    """The settings refuse AGENDA_DEV under systemd (INVOCATION_ID, which a scope gets too).
    The e2e is a dev-mode harness, on its own throwaway database: its scope alone drops the
    variable, nothing else does. The app units keep it, and AGENDA_ENV=production as well."""
    argv = shlex.split(deploy.remote_e2e_command("/var/lib/agenda-e2e/run.x"))
    i = argv.index("env")
    assert argv[i:i + 3] == ["env", "-u", "INVOCATION_ID"] and "AGENDA_ENV=production" not in argv
    app = (REPO / "nix/hosts/agenda/app.nix").read_text()
    assert "INVOCATION_ID" not in app and 'AGENDA_ENV = "production"' in app


def test_the_upload_and_cleanup_commands() -> None:
    upload = deploy.upload_e2e_tree_command()
    assert "mktemp -d /var/lib/agenda-e2e/run.XXXXXX" in upload and "tar -x" in upload
    assert deploy.cleanup_e2e_command("/var/lib/agenda-e2e/run.abc123") == "rm -rf /var/lib/agenda-e2e/run.abc123"
    with pytest.raises(ValueError):
        deploy.cleanup_e2e_command("/")


def test_the_remote_command_runs_as_written(tmp_path) -> None:
    """The real string, through a shell, with systemd-run and nix replaced by recorders: the
    words they receive are the ones the host would."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("systemd-run", "nix"):
        # Shell built-ins only: the build sandbox has no coreutils on /usr/bin:/bin.
        (bin_dir / name).write_text('#!/bin/sh\nprintf "%s\\n" "${0##*/}" "$@" >> "$LOG"\n')
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "log"
    path = "/var/lib/agenda-e2e/run.abc 123"
    subprocess.run(["sh", "-c", deploy.remote_e2e_command(path)], check=True,
                   env={"PATH": f"{bin_dir}:/usr/bin:/bin", "LOG": str(log)})
    words = log.read_text().splitlines()
    assert words[:3] == ["systemd-run", "--scope", "--quiet"] and f"AGENDA_E2E_ROOT={path}" in words
    assert words[-3:] == ["nix", "run", f"path:{path}#e2e"]



def test_the_flake_e2e_wrapper_takes_its_root_from_the_environment() -> None:
    flake = (REPO / "flake.nix").read_text()
    wrapper = flake[flake.index("e2e = pkgs.writeShellApplication"):flake.index("checkAll =")]
    assert "git rev-parse" not in wrapper and "AGENDA_E2E_ROOT" in wrapper
