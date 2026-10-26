"""The one-command deploy (spec, Operations: Deploy; plan, Task 16).

    nix run .#deploy

1. Refuse if the tree has uncommitted changes, if this isn't `main`, or if `main` differs
   from `origin/main` after fetching branches and tags (preflight, pure over a Snapshot).
2. The checks, the same as `nix run .#check`: `nix flake check`, the expand/contract migration
   lint (and the OpenAPI version check), then the offline e2e in Chromium. The e2e needs
   Linux: from a Mac it runs on the host, from the commit unpacked there, bounded.
3. Build the system on the host (`--store ssh-ng://`: a Mac never builds Linux), and refuse a
   closure with a browser in it.
4. deploy-rs, with magic rollback: the host reverts by itself unless the new generation
   confirms over a fresh SSH connection.
5. /healthz on the host, on gunicorn's loopback port (only loopback gets the details): a 200
   with "status": "ok" and "debug": false, or switch back to the previous generation.
6. Tag the commit deploy-<UTC YYYYMMDDTHHMMSSZ> and push the tag, the migration check's
   baseline for whoever deploys next.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

HOST_ATTR = ".#nixosConfigurations.agenda.config.system.build.toplevel"
HEALTHZ_TRIES = 30
BROWSERS = ("chromium", "playwright")


@dataclass(frozen=True)
class Snapshot:
    branch: str
    head: str
    origin_main: str | None
    dirty: list[str]


@dataclass(frozen=True)
class Result:
    code: int
    out: str = ""


Runner = Callable[[list[str]], Result]

# One deploy at a time on the host: an flock held by an SSH session for the build, the
# activation, /healthz and the tag; it goes when the session's stdin closes (or the laptop
# drops), so a crashed deploy never leaves it behind.
LOCK_COMMAND = "exec flock -n /run/agenda-deploy.lock sh -c 'echo locked; read -r _'"


@dataclass
class LockHeld:
    acquired: bool
    why: str = ""

    def release(self) -> None:
        pass


class HostLock:
    acquired = True
    why = ""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc

    def release(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def ssh_lock(ssh_argv: list[str]) -> HostLock | LockHeld:
    proc = subprocess.Popen([*ssh_argv, LOCK_COMMAND], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    if proc.stdout.readline().strip() == "locked":
        return HostLock(proc)
    proc.wait()
    return LockHeld(False, "another deploy holds /run/agenda-deploy.lock on the host (or SSH failed)")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout.strip()


def snapshot(repo: Path) -> Snapshot:
    """The git state preflight judges (after the fetch). Untracked files don't count: a flake
    only sees tracked ones."""
    origin = _git(repo, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main") or None
    return Snapshot(
        branch=_git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        head=_git(repo, "rev-parse", "HEAD"),
        origin_main=origin,
        dirty=[l for l in _git(repo, "status", "--porcelain", "--untracked-files=no").splitlines() if l.strip()],
    )


def preflight(snap: Snapshot) -> list[str]:
    """The reasons to refuse the deploy; empty when it may go ahead."""
    reasons = []
    if snap.dirty:
        reasons.append(f"uncommitted changes ({len(snap.dirty)} file(s)): commit or stash them first")
    if snap.branch != "main":
        reasons.append(f"on branch {snap.branch!r}, not main")
    if snap.origin_main is None:
        reasons.append("no origin/main: fetch failed or no remote")
    elif snap.head != snap.origin_main:
        reasons.append("main differs from origin/main: pull or push first")
    return reasons


# On the host the e2e (Chromium, a PostgreSQL, the app) shares 4 GB with production: in a
# bounded scope, at low CPU priority, so the OOM killer picks it before the live database.
E2E_LIMITS = ["MemoryMax=1200M", "MemorySwapMax=0", "CPUWeight=20"]


E2E_DIR = "/var/lib/agenda-e2e"


def upload_e2e_tree_command() -> str:
    """The host side of `git archive HEAD | ssh host <this>`: unpack the commit into a fresh
    root-only directory and print its path. A
    tarball, not `nix flake archive`: copying the source into the host's store failed with a
    hash mismatch between Determinate Nix and the host's Nix (2026-10-26)."""
    return (f"umask 077 && mkdir -p {E2E_DIR} && d=$(mktemp -d {E2E_DIR}/run.XXXXXX) && "
            f"tar -x -C \"$d\" && echo \"$d\"")


def cleanup_e2e_command(path: str) -> str:
    if not path.startswith(E2E_DIR + "/run.") or "/.." in path:
        raise ValueError(f"not an e2e tree: {path!r}")
    return f"rm -rf {shlex.quote(path)}"


def remote_e2e_command(path: str) -> str:
    """The shell command the host runs for the e2e from a Mac: the commit unpacked at `path`
    is both the flake that runs and the e2e's root.

    A scope (`systemd-run --scope`), so its output comes straight back over SSH. The settings
    refuse AGENDA_DEV under systemd, by INVOCATION_ID, which a scope gets too: the e2e is a
    dev-mode harness on its own throwaway database and port, so this one command drops the
    variable (`env -u`). Every app unit keeps it, and AGENDA_ENV=production refuses dev mode
    there regardless."""
    argv = ["systemd-run", "--scope", "--quiet", "--collect"]
    for prop in E2E_LIMITS:
        argv += ["-p", prop]
    argv += ["--", "env", "-u", "INVOCATION_ID", f"AGENDA_E2E_ROOT={path}",
             "nix", "run", f"path:{path}#e2e"]
    return " ".join(shlex.quote(a) for a in argv)


def healthy(body: str) -> bool:
    try:
        data = json.loads(body)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("status") == "ok" and data.get("debug") is False


def _site() -> tuple[str, Path]:
    """The host's address and pinned host key, from the environment the Nix app sets."""
    return os.environ.get("AGENDA_HOST", "51.15.79.107"), Path(os.environ.get("AGENDA_KNOWN_HOSTS", "nix/hosts/agenda/known_hosts"))


def run(repo: Path, *, runner: Runner, linux: bool, lock: Callable[[], HostLock | LockHeld] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], str] = lambda: datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        log: Callable[[str], None] = lambda l: print(l, flush=True)) -> int:
    address, known_hosts = _site()
    ssh_opts = ["-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes"]
    if key := os.environ.get("AGENDA_SSH_KEY"):
        ssh_opts += ["-i", key]
    host = f"root@{address}"

    def ssh(command: str) -> Result:
        return runner(["ssh", *ssh_opts, host, command])

    def step(name: str, cmd: list[str]) -> Result:
        log(f"== {name}")
        res = runner(cmd)
        if res.code != 0:
            log(f"   FAILED: {name}" + (f"\n{res.out.strip()[-2000:]}" if res.out.strip() else ""))
        return res

    # 1. Git.
    fetched = runner(["git", "-C", str(repo), "fetch", "--quiet", "--tags", "--prune", "origin"])
    if fetched.code != 0:
        log(f"Deploy refused: cannot fetch origin, so main can't be compared with it.\n{fetched.out.strip()}")
        return 1
    if reasons := preflight(snapshot(repo)):
        log("Deploy refused:\n" + "\n".join(f"  - {r}" for r in reasons))
        return 1
    commit = _git(repo, "rev-parse", "--short=12", "HEAD")

    # 2. Checks.
    env_ssh = "ssh " + " ".join(shlex.quote(o) for o in ssh_opts)
    if step("checks: nix flake check", ["nix", "flake", "check", "-L"]).code != 0:
        return _refused(log)
    if step("checks: migration lint and API version", ["nix", "run", ".#check-migrations"]).code != 0:
        return _refused(log)
    if linux:
        e2e = step("checks: offline e2e (Chromium)", ["nix", "run", ".#e2e"])
    else:
        # The commit, unpacked on the host; the e2e runs there from it, then it goes.
        ssh_cmd = " ".join(shlex.quote(w) for w in ["ssh", *ssh_opts, host, upload_e2e_tree_command()])
        src = step("checks: send the commit to the host for the e2e",
                   ["sh", "-c", f"git -C {shlex.quote(str(repo))} archive --format=tar HEAD | {ssh_cmd}"])
        if src.code != 0:
            return _refused(log)
        tree = src.out.strip().splitlines()[-1] if src.out.strip() else ""
        try:
            cleanup = cleanup_e2e_command(tree)
        except ValueError:
            log(f"   FAILED: no e2e directory from the host: {src.out.strip()[:300]!r}")
            return _refused(log)
        log("== checks: offline e2e (Chromium), on the host, bounded")
        e2e = ssh(remote_e2e_command(tree))
        ssh(cleanup)
        if e2e.code != 0:
            log(f"   FAILED: e2e\n{e2e.out.strip()[-2000:]}")
    if e2e.code != 0:
        return _refused(log)

    # 3-6 under the host's deploy lock (main() passes ssh_lock; tests pass their own).
    held = lock() if lock else LockHeld(True)
    if not held.acquired:
        log(f"Deploy refused: {held.why}.")
        return 1
    try:
        return _activate(repo, runner=runner, ssh=ssh, step=step, ssh_opts=ssh_opts, host=host, commit=commit,
                         sleep=sleep, now=now, log=log)
    finally:
        held.release()


def _activate(repo: Path, *, runner: Runner, ssh: Callable[[str], Result],
              step: Callable[[str, list[str]], Result], ssh_opts: list[str], host: str,
              commit: str, sleep: Callable[[float], None], now: Callable[[], str],
              log: Callable[[str], None]) -> int:
    # 3. Build on the host.
    built = step("build the system on the host",
                 ["env", f"NIX_SSHOPTS={' '.join(ssh_opts)}", "nix", "build", "--no-link", "--print-out-paths",
                  "--eval-store", "auto", "--store", f"ssh-ng://{host}", HOST_ATTR])
    if built.code != 0:
        return _refused(log)
    system = built.out.strip().splitlines()[-1] if built.out.strip() else ""
    closure = ssh(f"nix path-info -r {shlex.quote(system)}")
    found = [p for p in closure.out.splitlines() if any(b in p.lower() for b in BROWSERS)]
    if closure.code != 0 or found:
        log("Deploy refused: " + (f"a browser in the system closure: {found[:3]}" if found else "closure unreadable"))
        return 1

    # 4. Activate.
    previous = ssh("readlink -f /nix/var/nix/profiles/system").out.strip()
    log(f"== deploy {commit} (previous generation {previous or 'unknown'})")
    deployed = runner(["nix", "run", ".#deploy-rs", "--", "--skip-checks", f"--ssh-opts={' '.join(ssh_opts)}", ".#agenda"])
    if deployed.code != 0:
        log("   FAILED: deploy-rs. Its magic rollback has put the previous generation back.\n"
            + deployed.out.strip()[-2000:])
        return 1

    # 5. Health.
    log("== /healthz on the host")
    body = ""
    for _ in range(HEALTHZ_TRIES):
        res = ssh("curl -sS -m 10 -H 'Host: 127.0.0.1' http://127.0.0.1:8000/healthz")
        body = res.out.strip()
        if res.code == 0 and healthy(body):
            break
        sleep(2)
    else:
        log(f"   unhealthy: {body[:500] or 'no answer'}. Rolling back to {previous}.")
        if not previous.startswith("/nix/store/"):
            log("   previous generation unknown: roll back by hand (docs/ops/RUNBOOK.md, Rollback).")
            return 1
        back = ssh(f"nix-env -p /nix/var/nix/profiles/system --set {shlex.quote(previous)} && "
                   f"{shlex.quote(previous)}/bin/switch-to-configuration switch")
        log("   rolled back." if back.code == 0 else f"   ROLLBACK FAILED: {back.out.strip()[-1000:]}")
        return 1
    log(f"   {body}")

    # 6. Tag.
    tag = f"deploy-{now()}"
    for cmd in (["git", "-C", str(repo), "tag", tag, "HEAD"], ["git", "-C", str(repo), "push", "--quiet", "origin", tag]):
        res = runner(cmd)
        if res.code != 0:
            log(f"Deployed, but `{' '.join(cmd[3:])}` failed: {res.out.strip()}. Push the tag by hand: "
                f"git push origin {tag}")
            return 1
    log(f"Deployed {commit} as {tag}.")
    return 0


def _refused(log: Callable[[str], None]) -> int:
    log("Deploy refused: a check failed. Nothing was changed on the server.")
    return 1


def _subprocess(cmd: list[str]) -> Result:
    """Run `cmd`. Result.out is its stdout only (what gets parsed: /healthz, the previous
    generation, store paths); stderr (ssh warnings, nix progress) goes to the terminal."""
    quiet = any(q in " ".join(cmd) for q in ("path-info", "readlink", "archive --format=tar"))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    out = []
    assert proc.stdout is not None
    for line in proc.stdout:
        out.append(line)
        if not quiet:
            sys.stdout.write(line)
    return Result(proc.wait(), "".join(out))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    repo = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel") or Path.cwd())
    os.chdir(repo)
    # --remote-e2e: the Mac's path (the e2e on the host) from a Linux machine, to test it.
    linux = platform.system() == "Linux" and "--remote-e2e" not in argv
    address, known_hosts = _site()
    ssh_argv = ["ssh", "-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes"]
    if key := os.environ.get("AGENDA_SSH_KEY"):
        ssh_argv += ["-i", key]
    ssh_argv.append(f"root@{address}")
    return run(repo, runner=_subprocess, linux=linux, lock=lambda: ssh_lock(ssh_argv))


if __name__ == "__main__":
    sys.exit(main())
