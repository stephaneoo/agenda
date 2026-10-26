"""The scripted restore (spec, Operations: Restore is scripted).

    nix run .#restore -- [--target-time <RFC 3339> | --set <label>] [--test-only]

runs `agenda-restore` on the host over SSH; the monthly restore-test timer runs
`agenda-restore --test-only` there. Same steps either way:

1. Restore into a fresh scratch dataset, tank/pg-restore, with a temporary PostgreSQL
   instance (port 5433, its own socket, archive_mode off). The live dataset is never written.
2. Sanity checks (`manage.py check_restore`): the migrations match the deployed code, the
   canonical tables are non-empty, the latest claim is no newer than the target.
   --test-only stops here: the temporary instance is stopped and the scratch dataset dropped.
3. Snapshot the live dataset.
4. Stop the app, swap the datasets (the live one becomes tank/pg-prev-<stamp>), start the
   app, check /healthz.
5. If anything fails from 3 on, every step done is undone in reverse: the datasets swap back.
   The previous dataset is kept; destroying it (after 7 days) waits for Pierre.
Then a full backup, so the new timeline has a base backup (its failure is reported, the
restore stays).

plan(args) is pure; execute(steps, runner) runs them through `runner` (a shell on the host).
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable

LIVE = "tank/pg"
SCRATCH = "tank/pg-restore"
SCRATCH_DIR = "/var/lib/pg-restore"
SCRATCH_DATA = f"{SCRATCH_DIR}/16"
LIVE_DIR = "/var/lib/postgresql"
SOCKET = "/run/pg-restore"
PORT = 5433
APP_UNITS = "agenda-web.socket agenda-web.service sync-datatourisme.timer pgbackrest-backup.timer " \
            "pgbackrest-tier-expire.timer restore-test.timer"
APP_START = "agenda-web.socket agenda-web.service sync-datatourisme.timer pgbackrest-backup.timer " \
            "pgbackrest-tier-expire.timer restore-test.timer"
LABEL = re.compile(r"^\d{8}-\d{6}F(_\d{8}-\d{6}[DI])?$")
PG = f"runuser -u postgres --"
HEALTHZ = (
    "for i in $(seq 1 30); do "
    "body=$(curl -fsS -H 'Host: 127.0.0.1' http://127.0.0.1:8000/healthz) && "
    "echo \"$body\" | grep -q '\"status\": \"ok\"' && echo \"$body\" | grep -q '\"debug\": false' && "
    "{ echo \"$body\"; exit 0; }; sleep 2; done; echo \"healthz: ${body:-no answer}\" >&2; exit 1"
)


@dataclass(frozen=True)
class Args:
    target_time: str | None = None
    set: str | None = None
    test_only: bool = False


@dataclass(frozen=True)
class Step:
    name: str
    run: str
    undo: str | None = None
    # Always run at the end, success or not, after any undo.
    cleanup: bool = False
    # A failure is reported, never undone (the restore itself already succeeded).
    critical: bool = True


def _rfc3339(value: str) -> str:
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an RFC 3339 date-time: {value!r}") from None
    if t.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value!r} needs a UTC offset, e.g. 2026-11-02T21:00:00+01:00")
    return value


def _label(value: str) -> str:
    if not LABEL.match(value):
        raise argparse.ArgumentTypeError(f"not a pgBackRest backup label: {value!r}")
    return value


def parse_args(argv: list[str]) -> Args:
    p = argparse.ArgumentParser(prog="restore", description=__doc__.split("\n\n")[0])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--target-time", type=_rfc3339, help="RFC 3339, with offset")
    g.add_argument("--set", type=_label, help="a backup label (pgbackrest info)")
    p.add_argument("--test-only", action="store_true", help="steps 1-2 only: restore and check, then drop")
    a = p.parse_args(argv)
    return Args(a.target_time, a.set, a.test_only)


def plan(args: Args, *, stamp: str) -> list[Step]:
    """The steps, as shell commands run as root on the host. `stamp` names the previous
    dataset (UTC, YYYYMMDDTHHMMSSZ)."""
    prev = f"tank/pg-prev-{stamp}"
    prev_dir = f"/var/lib/pg-prev-{stamp}"
    target = []
    if args.target_time:
        target = ["--type=time", f"--target={shlex.quote(args.target_time)}", "--target-action=promote"]
    elif args.set:
        target = [f"--set={args.set}", "--type=immediate", "--target-action=promote"]
    conf = "\n".join([
        f"port = {PORT}",
        "listen_addresses = ''",
        f"unix_socket_directories = '{SOCKET}'",
        "archive_mode = off",
        "shared_buffers = 128MB",
        f"hba_file = '{SCRATCH_DIR}/pg_hba.conf'",
        f"ident_file = '{SCRATCH_DIR}/pg_ident.conf'",
    ])
    db_url = f"postgis://agenda@%2Frun%2Fpg-restore:{PORT}/agenda"
    sanity = f"agenda-manage --database-url={db_url} check_restore"
    if args.target_time:
        sanity += f" --target {shlex.quote(args.target_time)}"
    stop_scratch = (f"if [ -f {SCRATCH_DATA}/postmaster.pid ]; then "
                    f"{PG} pg_ctl -D {SCRATCH_DATA} -m fast -w stop; fi")

    steps = [
        Step("prepare the scratch dataset",
             f"{stop_scratch}; if zfs list -H {SCRATCH} >/dev/null 2>&1; then zfs destroy -r {SCRATCH}; fi && "
             f"zfs create -o mountpoint={SCRATCH_DIR} -o recordsize=8K -o com.sun:auto-snapshot=false {SCRATCH} && "
             f"chown postgres:postgres {SCRATCH_DIR} && chmod 0700 {SCRATCH_DIR} && "
             f"install -d -o postgres -g postgres -m 0700 {SCRATCH_DATA} && "
             f"install -d -o postgres -g postgres -m 0755 {SOCKET}"),
        Step("restore into the scratch dataset",
             "agenda-egress-refresh >/dev/null && " + " ".join([PG, "pgbackrest", "--stanza=agenda", f"--pg1-path={SCRATCH_DATA}", "--archive-mode=off",
                       *target, "restore"])),
        Step("configure the temporary instance",
             f"rm -f {SCRATCH_DATA}/postgresql.conf && printf '%s\\n' {shlex.quote(conf)} > {SCRATCH_DATA}/postgresql.conf && "
             f"echo 'local all all peer' > {SCRATCH_DIR}/pg_hba.conf && : > {SCRATCH_DIR}/pg_ident.conf && "
             f"chown postgres:postgres {SCRATCH_DATA}/postgresql.conf {SCRATCH_DIR}/pg_hba.conf {SCRATCH_DIR}/pg_ident.conf"),
        Step("start the temporary instance and wait for recovery",
             f"{{ {PG} pg_ctl -D {SCRATCH_DATA} -l {SCRATCH_DIR}/restore.log -w -t 3600 start || "
             f"{{ tail -n 20 {SCRATCH_DIR}/restore.log >&2; exit 1; }}; }}; "
             f"for i in $(seq 1 1800); do "
             f"[ \"$({PG} psql -h {SOCKET} -p {PORT} -d agenda -Atc 'select pg_is_in_recovery()')\" = f ] && exit 0; "
             f"sleep 2; done; echo 'still in recovery after 1 h' >&2; exit 1"),
        Step("sanity checks", sanity),
    ]
    if args.test_only:
        return steps + [
            Step("stop the temporary instance", stop_scratch, cleanup=True),
            Step("drop the scratch dataset",
                 f"if zfs list -H {SCRATCH} >/dev/null 2>&1; then zfs destroy -r {SCRATCH}; fi", cleanup=True),
        ]
    return steps + [
        Step("stop the temporary instance", f"{PG} pg_ctl -D {SCRATCH_DATA} -m fast -w stop"),
        Step("snapshot the live dataset", f"zfs snapshot {LIVE}@pre-restore-{stamp}"),
        Step("stop the app", f"systemctl stop {APP_UNITS}", undo=f"systemctl start {APP_START}"),
        Step("stop postgresql", "systemctl stop postgresql.service", undo="systemctl start postgresql.service"),
        Step("swap datasets",
             f"zfs set mountpoint={prev_dir} {LIVE} && zfs rename {LIVE} {prev} && "
             f"zfs set com.sun:auto-snapshot=false {prev} && "
             f"zfs set mountpoint={LIVE_DIR} {SCRATCH} && zfs rename {SCRATCH} {LIVE} && "
             f"zfs set com.sun:auto-snapshot=true {LIVE} && rm -f {LIVE_DIR}/16/postgresql.conf",
             undo=f"zfs set mountpoint={SCRATCH_DIR} {LIVE} && zfs rename {LIVE} {SCRATCH} && "
                  f"zfs set com.sun:auto-snapshot=false {SCRATCH} && "
                  f"zfs set mountpoint={LIVE_DIR} {prev} && zfs rename {prev} {LIVE} && "
                  f"zfs set com.sun:auto-snapshot=true {LIVE}"),
        Step("start postgresql", "systemctl start postgresql.service agenda-db-setup.service",
             undo="systemctl stop postgresql.service"),
        Step("start the app", f"systemctl restart agenda-migrate.service && systemctl start {APP_START}",
             undo=f"systemctl stop {APP_UNITS}"),
        Step("check /healthz", HEALTHZ),
        Step("full backup on the new timeline", "agenda-backup run", critical=False),
    ]


def execute(steps: list[Step], runner: Callable[[str], int], log: Callable[[str], None] = lambda _: None,
            ) -> tuple[bool, list[str]]:
    """Run the steps in order. On a critical failure, undo the steps done, newest first. The
    cleanup steps run last either way. Returns (success, log lines)."""
    lines: list[str] = []

    def say(line: str) -> None:
        lines.append(line)
        log(line)

    done: list[Step] = []
    ok = True
    swapped = False
    for step in steps:
        if step.cleanup:
            continue
        t0 = time.monotonic()
        say(f"-- {step.name}")
        if runner(step.run) != 0:
            if not step.critical:
                say(f"   FAILED (reported, not undone): {step.name}")
                continue
            say(f"   FAILED: {step.name}")
            ok = False
            undo_failed = False
            for d in reversed(done):
                if not d.undo:
                    continue
                say(f"-- undo: {d.name}")
                if runner(d.undo) != 0:
                    say(f"   UNDO FAILED: {d.name}. Stop here and call Pierre.")
                    undo_failed = True
                    break
            if swapped and not undo_failed:
                say("swapped back: the live dataset is in place again; the snapshot tank/pg@pre-restore-* is kept")
            break
        say(f"   ok ({time.monotonic() - t0:.1f} s)")
        done.append(step)
        swapped = swapped or step.name == "swap datasets"
    for step in steps:
        if step.cleanup:
            say(f"-- {step.name}")
            if runner(step.run) != 0:
                say(f"   FAILED: {step.name}")
    if ok and swapped:
        say("restored. The previous dataset is kept (tank/pg-prev-*, see `zfs list`); "
            "destroying it, after 7 days, waits for Pierre.")
    return ok, lines


def _shell(cmd: str) -> int:
    # --norc: NixOS bash reads /etc/bashrc in a shell started over SSH, which set -u trips on.
    return subprocess.run(["bash", "--norc", "--noprofile", "-euo", "pipefail", "-c", cmd]).returncode


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    t0 = time.monotonic()
    ok, _ = execute(plan(args, stamp=stamp), _shell, log=lambda l: print(l, flush=True))
    took = time.monotonic() - t0
    what = args.set or args.target_time or "the latest backup"
    print(f"{'test restore' if args.test_only else 'restore'} of {what}: "
          f"{'sanity checks passed' if ok else 'FAILED'} in {took:.0f} s", flush=True)
    if args.test_only:
        outcome = "ok" if ok else "failed"
        summary = f"restore test of {what}: {'sanity checks passed' if ok else 'failed'} in {took:.0f} s"
        subprocess.run(["runuser", "-u", "agenda", "--", "agenda-status", "record", "restore-test", outcome, summary],
                       check=False)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
