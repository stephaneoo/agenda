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
"""

from __future__ import annotations

import fcntl
import json
import os
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
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def path() -> Path:
    return Path(settings.STATUS_FILE)


def read() -> dict[str, Any]:
    """The file's content; an empty one if it is missing or unreadable."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": VERSION, "jobs": {}}
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
        return {"version": VERSION, "jobs": {}}
    return data


@contextmanager
def _locked(target: Path) -> Iterator[None]:
    with open(target.with_name(target.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


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
    target.parent.mkdir(parents=True, exist_ok=True)
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
