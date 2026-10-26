"""The one-command deploy (spec, Operations: Deploy; plan, Task 16).

    nix run .#deploy

1. Refuse if the tree has uncommitted changes, if this isn't `main`, or if `main` differs
   from `origin/main` after fetching branches and tags (preflight, pure over a Snapshot).
2. The checks, the same as `nix run .#check`: `nix flake check`, the expand/contract migration
   lint (and the OpenAPI version check), then the offline e2e in Chromium. The e2e needs
   Linux: from a Mac it runs on the host, from the flake source copied there.
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


def healthy(body: str) -> bool:
    try:
        data = json.loads(body)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("status") == "ok" and data.get("debug") is False


def _site() -> tuple[str, Path]:
    """The host's address and pinned host key, from the environment the Nix app sets."""
    return os.environ.get("AGENDA_HOST", "51.15.79.107"), Path(os.environ.get("AGENDA_KNOWN_HOSTS", "nix/hosts/agenda/known_hosts"))


def run(repo: Path, *, runner: Runner, linux: bool, sleep: Callable[[float], None] = time.sleep,
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
        # The flake's source and inputs, copied to the host's store; the e2e runs there.
        src = step("checks: copy the flake to the host for the e2e",
                   ["env", f"NIX_SSHOPTS={' '.join(ssh_opts)}", "nix", "flake", "archive", "--json",
                    "--to", f"ssh-ng://{host}"])
        if src.code != 0:
            return _refused(log)
        try:
            path = json.loads(src.out)["path"]
        except (ValueError, KeyError):
            path = ""
        log("== checks: offline e2e (Chromium), on the host")
        e2e = ssh(f"nix run {shlex.quote(path + '#e2e')}")
        if e2e.code != 0:
            log(f"   FAILED: e2e\n{e2e.out.strip()[-2000:]}")
    if e2e.code != 0:
        return _refused(log)

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
    quiet = any(q in " ".join(cmd) for q in ("path-info", "readlink", "flake archive"))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    out = []
    assert proc.stdout is not None
    for line in proc.stdout:
        out.append(line)
        if not quiet:
            sys.stdout.write(line)
    return Result(proc.wait(), "".join(out))


def main() -> int:
    repo = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel") or Path.cwd())
    os.chdir(repo)
    return run(repo, runner=_subprocess, linux=platform.system() == "Linux")


if __name__ == "__main__":
    sys.exit(main())
