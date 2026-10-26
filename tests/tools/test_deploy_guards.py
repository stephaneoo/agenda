"""The guarded deploy (tools/deploy.py, spec: Deploy). Git is a real temp repo with a bare
origin; everything that would reach the host is a stub runner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools import deploy
from tools.check_migrations import DEPLOY_TAG


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    git(work, "config", "user.email", "t@example.org")
    git(work, "config", "user.name", "t")
    (work / "a.txt").write_text("a\n")
    git(work, "add", "a.txt")
    git(work, "commit", "-q", "-m", "first")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "main")
    return work


def test_clean_repo_in_step_with_origin_passes(repo) -> None:
    assert deploy.preflight(deploy.snapshot(repo)) == []


def test_refuses_dirty_tree(repo) -> None:
    (repo / "a.txt").write_text("changed\n")
    reasons = deploy.preflight(deploy.snapshot(repo))
    assert any("uncommitted" in r for r in reasons)
    git(repo, "add", "a.txt")  # staged counts too
    assert any("uncommitted" in r for r in deploy.preflight(deploy.snapshot(repo)))


def test_untracked_files_do_not_block(repo) -> None:
    (repo / "notes.txt").write_text("x\n")  # flakes only see tracked files
    assert deploy.preflight(deploy.snapshot(repo)) == []


def test_refuses_when_main_differs_from_origin(repo) -> None:
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "local only")
    assert any("origin/main" in r for r in deploy.preflight(deploy.snapshot(repo)))


def test_refuses_another_branch(repo) -> None:
    git(repo, "checkout", "-q", "-b", "feature")
    assert any("branch" in r for r in deploy.preflight(deploy.snapshot(repo)))


def test_preflight_is_pure() -> None:
    snap = deploy.Snapshot(branch="main", head="a" * 40, origin_main="b" * 40, dirty=["M a.txt"])
    assert len(deploy.preflight(snap)) == 2
    assert deploy.preflight(deploy.Snapshot(branch="main", head="a" * 40, origin_main="a" * 40, dirty=[])) == []
    assert deploy.preflight(deploy.Snapshot(branch="main", head="a" * 40, origin_main=None, dirty=[]))


# --- The run, with a stub runner -----------------------------------------------------------------

HEALTHY = json.dumps({"status": "ok", "database": True, "migrations": True, "debug": False})


class Stub:
    """Answers each command by the first matching rule; records every command."""

    def __init__(self, rules: dict[str, tuple[int, str]] | None = None) -> None:
        self.rules = {"healthz": (0, HEALTHY), "readlink": (0, "/nix/store/prev-system"),
                      "--print-out-paths": (0, "/nix/store/new-system"), **(rules or {})}
        self.calls: list[str] = []

    def __call__(self, cmd: list[str]) -> deploy.Result:
        line = " ".join(cmd)
        self.calls.append(line)
        # A git command is matched on what follows `git -C <repo>` (the temp path holds the
        # test's name).
        match = " " + " ".join(cmd[3:]) + " " if cmd[0] == "git" else line
        for needle, (code, out) in self.rules.items():
            if needle in match:
                return deploy.Result(code, out)
        if cmd[0] == "git":  # git is real: the temp repo and its origin
            res = subprocess.run(cmd, capture_output=True, text=True)
            return deploy.Result(res.returncode, res.stdout + res.stderr)
        return deploy.Result(0, "")

    def called(self, needle: str) -> bool:
        return any(needle in c for c in self.calls)

    def index(self, needle: str) -> int:
        return next(i for i, c in enumerate(self.calls) if needle in c)


def run(repo, stub, linux=True):
    return deploy.run(repo, runner=stub, linux=linux, sleep=lambda _: None, now=lambda: "20261026T101500Z")


def test_refuses_when_checks_fail(repo) -> None:
    stub = Stub({"flake check": (1, "pytest failed")})
    assert run(repo, stub) == 1
    assert not stub.called("deploy-rs") and not stub.called(" tag deploy-")


def test_refuses_when_only_the_migration_lint_fails(repo) -> None:
    stub = Stub({"check-migrations": (1, "1 violation")})
    assert run(repo, stub) == 1
    assert stub.called("flake check") and not stub.called("deploy-rs")


def test_refuses_when_the_e2e_fails_on_the_host_from_a_mac(repo) -> None:
    stub = Stub({"archive --format=tar": (0, "/var/lib/agenda-e2e/run.x"), "#e2e": (1, "offline banner missing")})
    assert run(repo, stub, linux=False) == 1
    assert stub.called("ssh") and not stub.called("deploy-rs")


def test_refuses_a_browser_in_the_closure(repo) -> None:
    stub = Stub({"path-info": (0, "/nix/store/abc-chromium-140\n/nix/store/def-glibc")})
    assert run(repo, stub) == 1
    assert not stub.called("deploy-rs")


def test_refuses_when_origin_cannot_be_fetched(repo) -> None:
    stub = Stub({" fetch ": (128, "Could not read from remote repository.")})
    assert run(repo, stub) == 1
    assert not any(c.startswith(("nix ", "ssh ", "env ")) for c in stub.calls)


def test_refuses_a_dirty_tree_before_anything_runs(repo) -> None:
    (repo / "a.txt").write_text("changed\n")
    stub = Stub()
    assert run(repo, stub) == 1
    assert not any(c.startswith(("nix ", "ssh ", "env ")) for c in stub.calls)


def test_tags_and_pushes_after_healthy(repo) -> None:
    stub = Stub()
    assert run(repo, stub) == 0
    order = ["flake check", "check-migrations", "#e2e", "--print-out-paths", "path-info", "deploy-rs", "healthz",
             " tag deploy-", " push --quiet origin deploy-"]
    assert [stub.index(n) for n in order] == sorted(stub.index(n) for n in order)
    tag = git(repo, "tag", "--list", "deploy-*")
    assert tag == "deploy-20261026T101500Z" and DEPLOY_TAG.match(tag)
    assert git(repo.parent / "origin.git", "tag", "--list") == tag  # pushed
    assert not stub.called("switch-to-configuration")


@pytest.mark.parametrize("answer", [(7, ""), (0, HEALTHY.replace("false", "true")), (0, "not json"),
                                    (0, json.dumps({"status": "error", "debug": False}))])
def test_rolls_back_when_healthz_fails(repo, answer) -> None:
    stub = Stub({"healthz": answer})
    assert run(repo, stub) == 1
    assert stub.called("deploy-rs")
    rollback = stub.calls[stub.index("switch-to-configuration")]
    assert "/nix/store/prev-system" in rollback
    assert git(repo, "tag", "--list") == ""


def test_no_extra_rollback_when_deploy_rs_already_rolled_back(repo) -> None:
    stub = Stub({"deploy-rs": (1, "magic rollback")})
    assert run(repo, stub) == 1
    assert not stub.called("switch-to-configuration") and not stub.called("healthz")


def test_command_output_keeps_stderr_apart() -> None:
    """ssh warnings go to stderr; the answer parsed (healthz, the previous generation) is stdout."""
    res = deploy._subprocess(["sh", "-c", "echo 'ssh: a warning' >&2; echo '{\"status\": \"ok\", \"debug\": false}'"])
    assert res.code == 0 and deploy.healthy(res.out) and "warning" not in res.out


def test_from_a_mac_the_e2e_runs_on_the_host_with_the_bounded_command(repo) -> None:
    tree = "/var/lib/agenda-e2e/run.abc123"
    stub = Stub({"archive --format=tar": (0, tree + "\n")})
    assert run(repo, stub, linux=False) == 0
    upload = stub.calls[stub.index("archive --format=tar")]
    assert "HEAD" in upload and deploy.upload_e2e_tree_command() in upload
    e2e = stub.calls[stub.index("#e2e")]
    assert e2e.startswith("ssh ") and deploy.remote_e2e_command(tree) in e2e
    assert stub.index("#e2e") < stub.index("rm -rf " + tree)  # removed afterwards
    assert not stub.called("nix run .#e2e")


def test_from_a_mac_a_failed_e2e_still_removes_the_tree(repo) -> None:
    tree = "/var/lib/agenda-e2e/run.abc123"
    stub = Stub({"archive --format=tar": (0, tree), "#e2e": (1, "boom")})
    assert run(repo, stub, linux=False) == 1
    assert stub.called("rm -rf " + tree) and not stub.called("deploy-rs")


def test_from_a_mac_a_bad_upload_refuses(repo) -> None:
    stub = Stub({"archive --format=tar": (0, "not a path")})
    assert run(repo, stub, linux=False) == 1
    assert not stub.called("#e2e") and not stub.called("deploy-rs")


def test_refuses_when_another_deploy_holds_the_host_lock(repo) -> None:
    stub = Stub()
    rc = deploy.run(repo, runner=stub, linux=True, sleep=lambda _: None, now=lambda: "20261026T101500Z",
                    lock=lambda: deploy.LockHeld(False, "held by another deploy"))
    assert rc == 1
    assert not stub.called("deploy-rs") and not stub.called("--print-out-paths")


def test_the_host_lock_is_held_across_build_activation_and_tag(repo) -> None:
    stub = Stub()
    events: list[str] = []

    class Lock:
        acquired = True
        why = ""

        def release(self) -> None:
            events.append(f"release after {len(stub.calls)} calls")

    def lock():
        events.append(f"acquire after {len(stub.calls)} calls")
        return Lock()

    assert deploy.run(repo, runner=stub, linux=True, sleep=lambda _: None, now=lambda: "20261026T101500Z",
                      lock=lock) == 0
    acquired = int(events[0].split()[-2])
    released = int(events[1].split()[-2])
    assert acquired <= stub.index("--print-out-paths") and released > stub.index(" push --quiet origin deploy-")


def test_the_lock_command_uses_flock_on_the_host() -> None:
    assert "flock -n" in deploy.LOCK_COMMAND and "/run/agenda-deploy.lock" in deploy.LOCK_COMMAND
