"""The status file: each timer job's last outcome, for the MOTD and `nix run .#status`
(spec, Status reporting). The format and the writing live in agenda_site.statusfile, which
the host's `agenda-status` CLI shares; this module points it at settings.STATUS_FILE. No external alerting: "alert" means recording failed or partial here.

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

from datetime import datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from django.utils import timezone

from agenda_site import statusfile
from agenda_site.statusfile import DIR_MODE, JOB, MAX_SUMMARY, OUTCOMES, SUCCESS, VERSION  # noqa: F401

_one_line = statusfile.one_line


def path() -> Path:
    return Path(settings.STATUS_FILE)


def read() -> dict[str, Any]:
    """The file's content; an empty one if it is missing or unreadable."""
    return statusfile.read(path())


def record(job: str, outcome: str, summary: str, *, now: datetime | None = None) -> None:
    """Rewrite `job`'s entry in settings.STATUS_FILE (agenda_site.statusfile.record). Raises
    ValueError for a bad job name or outcome, OSError if the file can't be written."""
    statusfile.record(path(), job, outcome, summary, now=now or timezone.now())
