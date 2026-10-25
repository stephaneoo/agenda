"""The status file: each timer job's last outcome, for the MOTD and `nix run .#status`
(spec, Status reporting). No external alerting: "alert" means recording failed or partial here.

    {"version": 1,
     "jobs": {"sync-datatourisme": {"outcome": "ok" | "partial" | "failed",
                                    "at": RFC 3339 UTC, "summary": one line,
                                    "last_success": RFC 3339 UTC | null}}}

`last_success` is the last ok or partial run: a job whose last_success is older than its
expected interval shows as STALE. record() rewrites one job's entry atomically (a temporary
file in the same directory, then rename), under an exclusive lock, so jobs finishing at the
same time don't lose each other's entries.

The directory is created 0750. record() refuses to write (logs and returns) when the
directory is group- or world-writable or owned by another user, and never follows a symlink
for the lock file. The summary is one line: no control, C1 or bidi characters.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import stat
import re
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from django.conf import settings
from django.utils import timezone

OUTCOMES = ("ok", "partial", "failed")
SUCCESS = ("ok", "partial")
VERSION = 1
MAX_SUMMARY = 300
JOB = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# C0, DEL and C1 controls, and the bidi embeddings, overrides, isolates and marks: a
# summary must not reorder or hide what the MOTD shows around it.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]+")
DIR_MODE = 0o750
logger = logging.getLogger(__name__)


def path() -> Path:
    return Path(settings.STATUS_FILE)


def read() -> dict[str, Any]:
    """The file's content; an empty one if it is missing or unreadable."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
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


def _one_line(summary: str) -> str:
    text = " ".join(_CONTROL.sub(" ", summary).split())
    return text if len(text) <= MAX_SUMMARY else text[: MAX_SUMMARY - 1] + "…"


def record(job: str, outcome: str, summary: str, *, now: datetime | None = None) -> None:
    """Rewrite `job`'s entry in settings.STATUS_FILE. Raises ValueError for a bad job name
    or outcome, OSError if the file can't be written."""
    if not isinstance(job, str) or not JOB.match(job):
        raise ValueError(f"bad job name {job!r}")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, not {outcome!r}")
    at = (now or timezone.now()).astimezone(UTC).isoformat()
    target = path()
    target.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    if why := _unsafe_dir(target.parent):
        logger.error("status file not written: %r is %s", str(target.parent), why)
        return
    with _locked(target):
        data = read()
        previous = data["jobs"].get(job) if isinstance(data["jobs"].get(job), dict) else {}
        data["version"] = VERSION
        data["jobs"][job] = {
            "outcome": outcome,
            "at": at,
            "summary": _one_line(summary),
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
