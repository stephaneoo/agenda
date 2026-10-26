"""Grandfather-father-son retention over pgBackRest (spec, Backups), and the host's
`agenda-backup` command.

pgBackRest's own expiry counts backups; it can't keep "a daily for 30 days, a weekly for 12
weeks, a monthly for 6 months". So every backup is a full one annotated `tier=<tier>`, and
`agenda-backup expire` deletes the sets past their tier's age with `expire --set`.

- tier_for(today, existing): the first backup of a month is monthly, any other Sunday weekly,
  everything else daily.
- sets_to_expire(sets, now): past AGES[tier]. Never expired: the newest
  ARCHIVE_RETENTION_FULL sets, every set that stopped within PROTECT (8 days), and the "base"
  (the newest set that stopped before the 7-day window opened, where its WAL replay starts).
- archive_retention(sets, now): how many of the newest full sets keep their WAL, so that it
  reaches back from the base: pgBackRest's --repo1-retention-archive, computed for each
  backup and expiry run. By time, not count, so DST (a 23 h day) and extra full backups (a
  restore, a manual one) never shorten the window.
Months and weekdays are Paris ones (a backup at 01:30 CEST stopped the day before in UTC).

    agenda-backup run       a full backup, annotated with today's tier (Europe/Paris)
    agenda-backup expire    expire the sets past their tier's age
Both record their outcome in the status file (pgbackrest-backup, pgbackrest-tier-expire).
They run as root (the timers) and drop to postgres for pgBackRest, to agenda for the status.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

AGES: dict[str, timedelta] = {
    "daily": timedelta(days=30),
    "weekly": timedelta(days=84),
    "monthly": timedelta(days=183),
}
# An unannotated set (a backup taken by hand) is kept as long as a monthly.
UNKNOWN_AGE = AGES["monthly"]
PITR = timedelta(days=7)
PROTECT = timedelta(days=8)
PARIS = ZoneInfo("Europe/Paris")
# One full backup a day: the newest 8 reach back past 7 days. Keep in step with
# repo1-retention-archive in nix/hosts/agenda/backup.nix.
ARCHIVE_RETENTION_FULL = 8
STANZA = "agenda"


@dataclass(frozen=True)
class BackupSet:
    label: str
    tier: str | None
    stop_time: datetime


def tier_for(today: date, existing: Iterable[BackupSet] | None = None) -> str:
    """The tier of a backup taken `today`. With the existing sets, "first of the month" means
    no monthly yet this month; without them, the 1st."""
    if existing is None:
        first = today.day == 1
    else:
        first = not any(
            s.tier == "monthly" and (lambda t: (t.year, t.month))(s.stop_time.astimezone(PARIS)) == (today.year, today.month)
            for s in existing
        )
    if first:
        return "monthly"
    return "weekly" if today.weekday() == 6 else "daily"


def _base(ordered: list[BackupSet], now: datetime) -> BackupSet | None:
    """The newest set that stopped before the PITR window opened: replay starts there."""
    before = [s for s in ordered if s.stop_time <= now - PITR]
    return before[-1] if before else None


def sets_to_expire(sets: Iterable[BackupSet], now: datetime) -> list[str]:
    ordered = sorted(sets, key=lambda s: s.stop_time)
    protected = {s.label for s in ordered[-ARCHIVE_RETENTION_FULL:]}
    protected |= {s.label for s in ordered if now - s.stop_time <= PROTECT}
    if base := _base(ordered, now):
        protected.add(base.label)
    return [
        s.label for s in ordered
        if s.label not in protected and now - s.stop_time > AGES.get(s.tier or "", UNKNOWN_AGE)
    ]


def archive_retention(sets: Iterable[BackupSet], now: datetime) -> int:
    """How many of the newest full sets keep their WAL: from the base on, at least
    ARCHIVE_RETENTION_FULL."""
    ordered = sorted(sets, key=lambda s: s.stop_time)
    base = _base(ordered, now)
    n = len(ordered) - ordered.index(base) if base else len(ordered)
    return max(ARCHIVE_RETENTION_FULL, n)


def pitr_start(sets: Iterable[BackupSet], now: datetime) -> datetime:
    """The earliest point in time recoverable: the stop of the oldest set whose WAL is kept."""
    ordered = sorted(sets, key=lambda s: s.stop_time)
    return ordered[-min(archive_retention(ordered, now), len(ordered))].stop_time


def simulate(days: Iterable[date], extra_days: Iterable[date] = ()) -> list[tuple[date, list[BackupSet]]]:
    """Back up at 01:30 Paris (and again at 14:00 on extra_days), then expire, on each of
    `days`, as the timers do: the sets after each day."""
    extra = set(extra_days)
    sets: list[BackupSet] = []
    history = []
    for day in days:
        stops = [datetime(day.year, day.month, day.day, 1, 30, tzinfo=PARIS)]
        if day in extra:
            stops.append(datetime(day.year, day.month, day.day, 14, 0, tzinfo=PARIS))
        for stop in stops:
            stop = stop.astimezone(UTC)
            sets.append(BackupSet(f"{stop:%Y%m%d-%H%M%S}F", tier_for(day, sets), stop))
            gone = set(sets_to_expire(sets, stop + timedelta(hours=1)))
            sets = [s for s in sets if s.label not in gone]
        history.append((day, list(sets)))
    return history


def parse_info(info: list[dict[str, Any]], stanza: str = STANZA) -> list[BackupSet]:
    """The full backup sets of `pgbackrest info --output=json`."""
    out = []
    for st in info:
        if st.get("name") != stanza:
            continue
        for b in st.get("backup", []):
            if b.get("type") != "full":
                continue
            tier = (b.get("annotation") or {}).get("tier")
            out.append(BackupSet(b["label"], tier if tier in AGES else None,
                                 datetime.fromtimestamp(b["timestamp"]["stop"], UTC)))
    return out


# --- On the host ---------------------------------------------------------------------------

def _pgbackrest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["runuser", "-u", "postgres", "--", "pgbackrest", f"--stanza={STANZA}", *args],
                          capture_output=True, text=True)


def _record(job: str, outcome: str, summary: str) -> None:
    subprocess.run(["runuser", "-u", "agenda", "--", "agenda-status", "record", job, outcome, summary], check=False)


def _info() -> tuple[list[BackupSet], list[dict[str, Any]]]:
    res = _pgbackrest("--output=json", "info")
    if res.returncode != 0:
        raise RuntimeError(_last_line(res))
    raw = json.loads(res.stdout)
    return parse_info(raw), raw


def _last_line(res: subprocess.CompletedProcess[str]) -> str:
    lines = [l for l in (res.stderr + res.stdout).splitlines() if l.strip()]
    return lines[-1] if lines else f"exit {res.returncode}"


def _paris_today() -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Europe/Paris")).date()


def run_backup(log: Callable[[str], None] = print) -> int:
    job = "pgbackrest-backup"
    try:
        sets, _ = _info()
        tier = tier_for(_paris_today(), sets)
        now = datetime.now(UTC)
        keep = archive_retention([*sets, BackupSet("(this one)", tier, now)], now)
        res = _pgbackrest("--type=full", f"--annotation=tier={tier}", f"--repo1-retention-archive={keep}", "backup")
        if res.returncode != 0:
            raise RuntimeError(_last_line(res))
        sets, raw = _info()
        newest = max(sets, key=lambda s: s.stop_time)
        size = next((b.get("info", {}).get("repository", {}).get("delta", 0)
                     for st in raw for b in st.get("backup", []) if b.get("label") == newest.label), 0)
        summary = f"full backup {newest.label}, tier {tier}, {size / 2**20:.1f} MiB in the repository; {len(sets)} sets"
    except Exception as e:  # recorded, then the unit fails
        _record(job, "failed", f"backup failed: {e}")
        log(f"backup failed: {e}")
        return 1
    _record(job, "ok", summary)
    log(summary)
    return 0


def run_expire(log: Callable[[str], None] = print) -> int:
    job = "pgbackrest-tier-expire"
    try:
        sets, _ = _info()
        now = datetime.now(UTC)
        gone = sets_to_expire(sets, now)
        keep = archive_retention([s for s in sets if s.label not in gone], now)
        for label in gone:
            res = _pgbackrest(f"--set={label}", f"--repo1-retention-archive={keep}", "expire")
            if res.returncode != 0:
                raise RuntimeError(f"expire {label}: {_last_line(res)}")
        # The WAL older than the base goes even when no set does.
        res = _pgbackrest(f"--repo1-retention-archive={keep}", "expire")
        if res.returncode != 0:
            raise RuntimeError(f"expire WAL: {_last_line(res)}")
        summary = (f"expired {len(gone)} set(s){': ' + ', '.join(gone) if gone else ''}; {len(sets) - len(gone)} kept, "
                   f"WAL kept for the newest {keep}")
    except Exception as e:
        _record(job, "failed", f"tier expiry failed: {e}")
        log(f"tier expiry failed: {e}")
        return 1
    _record(job, "ok", summary)
    log(summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["run"]:
        return run_backup()
    if argv == ["expire"]:
        return run_expire()
    print("usage: agenda-backup run|expire", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
