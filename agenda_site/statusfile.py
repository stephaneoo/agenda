"""The status file and its report, without Django: the `agenda-status` CLI on the host, the
MOTD, and `nix run .#status` all use this module; `agenda_site.status` wraps it for the app.

    {"version": 1,
     "jobs": {"sync-datatourisme": {"outcome": "ok" | "partial" | "failed",
                                    "at": RFC 3339 UTC, "summary": one line,
                                    "last_success": RFC 3339 UTC | null}}}

`last_success` is the last ok or partial run. A job in EXPECTED whose last success is older
than its interval (spec, Status reporting) shows as STALE. record() rewrites one job's entry
atomically (a temporary file in the same directory, then rename) under an exclusive lock.

The directory is created 0750. record() refuses to write (logs and returns) when the
directory is group- or world-writable or owned by another user, and never follows a symlink
for the lock file. Every string written or printed is one line: no C0, C1 or bidi controls.

    agenda-status [--file F] show [--no-system] [--check]
    agenda-status [--file F] record <job> <ok|partial|failed> <summary...>
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Iterator

OUTCOMES = ("ok", "partial", "failed")
SUCCESS = ("ok", "partial")
VERSION = 1
MAX_SUMMARY = 300
JOB = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# C0, DEL and C1 controls, and the bidi embeddings, overrides, isolates and marks: a
# summary must not reorder or hide what the MOTD shows around it.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩‎‏؜]+")
DIR_MODE = 0o750
DEFAULT_FILE = "/var/lib/agenda/status.json"

# Each timer job, and how long after its last success it shows as STALE.
EXPECTED: dict[str, timedelta] = {
    "sync-datatourisme": timedelta(hours=26),
    "pgbackrest-backup": timedelta(hours=26),
    "pgbackrest-tier-expire": timedelta(hours=26),
    "restore-test": timedelta(days=32),
}

logger = logging.getLogger("agenda_site.status")


def one_line(text: Any, limit: int = MAX_SUMMARY) -> str:
    text = " ".join(_CONTROL.sub(" ", str(text)).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def read(path: str | os.PathLike) -> dict[str, Any]:
    """The file's content; an empty one if it is missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {"version": VERSION, "jobs": {}}
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
        return {"version": VERSION, "jobs": {}}
    return data


@contextmanager
def _locked(target: Path) -> Iterator[None]:
    """An exclusive lock on <target>.lock, opened without following a symlink."""
    fd = os.open(target.with_name(target.name + ".lock"), os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _unsafe_dir(directory: Path) -> str | None:
    """Why `directory` can't be trusted with the status file, or None."""
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        return "not a directory"
    if info.st_uid != os.geteuid():
        return f"owned by uid {info.st_uid}, not {os.geteuid()}"
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"group- or world-writable (mode {stat.S_IMODE(info.st_mode):o})"
    return None


def record(path: str | os.PathLike, job: str, outcome: str, summary: str, *, now: datetime | None = None) -> None:
    """Rewrite `job`'s entry in the file at `path`. Raises ValueError for a bad job name or
    outcome, OSError if the file can't be written."""
    if not isinstance(job, str) or not JOB.match(job):
        raise ValueError(f"bad job name {job!r}")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, not {outcome!r}")
    at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    target = Path(path)
    target.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    if why := _unsafe_dir(target.parent):
        logger.error("status file not written: %r is %s", str(target.parent), why)
        return
    with _locked(target):
        data = read(target)
        previous = data["jobs"].get(job) if isinstance(data["jobs"].get(job), dict) else {}
        data["version"] = VERSION
        data["jobs"][job] = {
            "outcome": outcome,
            "at": at,
            "summary": one_line(summary),
            "last_success": at if outcome in SUCCESS else previous.get("last_success"),
        }
        fd, tmp = tempfile.mkstemp(prefix=".status-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        except BaseException:
            os.unlink(tmp)
            raise


# --- The report ------------------------------------------------------------------------------

def _paris() -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Europe/Paris")
    except Exception:  # no tz database: UTC, labelled as such
        return UTC


def _when(value: Any, zone: tzinfo) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(zone)


def _fmt(t: datetime | None) -> str:
    return t.strftime("%Y-%m-%d %H:%M") if t else "never"


def job_rows(data: dict[str, Any], now: datetime) -> list[tuple[str, str, str, str, str, bool]]:
    """(job, state, last run, last success, summary, needs attention) for every expected job
    and every other job in the file."""
    zone = _paris()
    jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
    names = list(EXPECTED) + sorted(n for n in jobs if n not in EXPECTED)
    rows = []
    for name in names:
        entry = jobs.get(name) if isinstance(jobs.get(name), dict) else {}
        outcome = entry.get("outcome")
        last_success = _when(entry.get("last_success"), zone)
        limit = EXPECTED.get(name)
        stale = limit is not None and (last_success is None or now - last_success > limit)
        state = {"ok": "ok", "partial": "PARTIAL", "failed": "FAILED"}.get(outcome, "never run" if not entry else "?")
        if stale:
            state = f"STALE ({state})" if entry else "STALE"
        attention = stale or outcome in ("partial", "failed")
        summary = entry.get("summary", "") if entry else "never run"
        rows.append((one_line(name, 64), state, _fmt(_when(entry.get("at"), zone)), _fmt(last_success),
                     one_line(summary), attention))
    return rows


def render(data: dict[str, Any], *, now: datetime, system: list[str], file_missing: bool = False) -> str:
    zone = _paris()
    label = "Europe/Paris" if zone is not UTC else "UTC"
    out = [f"agenda server status, {now.astimezone(zone):%Y-%m-%d %H:%M} {label}"]
    if file_missing or not data.get("jobs"):
        out.append("  (no status file yet, or it is empty: no job has recorded anything)")
    rows = job_rows(data, now)
    out.append(f"  {'job':<24} {'state':<20} {'last run':<17} {'last success':<17} summary")
    for name, state, at, ok, summary, _ in rows:
        out.append(f"  {name:<24} {state:<20} {at:<17} {ok:<17} {summary}")
    out.extend("  " + one_line(line, 200) for line in system)
    attention = [r[0] for r in rows if r[5]]
    out.append("  needs attention: " + ", ".join(attention) if attention else "  all jobs ok")
    return "\n".join(out) + "\n"


def system_lines() -> list[str]:
    """Disk usage of / and the PostgreSQL dataset, and the zpool's health."""
    lines = []
    for mount in ("/", "/var/lib/postgresql"):
        try:
            u = shutil.disk_usage(mount)
        except OSError:
            continue
        lines.append(f"disk {mount}: {u.used / 2**30:.1f} GiB used of {u.total / 2**30:.1f} GiB "
                     f"({100 * u.used / u.total:.0f}%), {u.free / 2**30:.1f} GiB free")
    zpool = shutil.which("zpool") or ("/run/current-system/sw/bin/zpool" if os.path.exists("/run/current-system/sw/bin/zpool") else None)
    if zpool:
        try:
            res = subprocess.run([zpool, "list", "-H", "-o", "name,size,alloc,free,cap,health"],
                                 capture_output=True, text=True, timeout=10)
            for row in res.stdout.splitlines():
                f = row.split("\t")
                if len(f) == 6:
                    lines.append(f"zpool {f[0]}: {f[5]}, {f[2]} used of {f[1]} ({f[4]}), {f[3]} free")
            if res.returncode != 0:
                lines.append(f"zpool list failed: {res.stderr.strip()}")
        except (OSError, subprocess.SubprocessError) as e:
            lines.append(f"zpool list failed: {e}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agenda-status")
    parser.add_argument("--file", default=os.environ.get("AGENDA_STATUS_FILE") or DEFAULT_FILE)
    sub = parser.add_subparsers(dest="command")
    show = sub.add_parser("show")
    show.add_argument("--no-system", action="store_true")
    show.add_argument("--check", action="store_true", help="exit 1 when a job needs attention")
    rec = sub.add_parser("record")
    rec.add_argument("job")
    rec.add_argument("outcome")
    rec.add_argument("summary", nargs="*")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if args.command == "record":
        try:
            record(args.file, args.job, args.outcome, " ".join(args.summary))
        except ValueError as e:
            print(f"agenda-status: {e}", file=sys.stderr)
            return 2
        return 0
    data = read(args.file)
    now = datetime.now(UTC)
    no_system = getattr(args, "no_system", False)
    sys.stdout.write(render(data, now=now, system=[] if no_system else system_lines(),
                            file_missing=not Path(args.file).exists()))
    if getattr(args, "check", False) and any(r[5] for r in job_rows(data, now)):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
