"""Grandfather-father-son retention over pgBackRest (tools/backup_tiers.py, spec: Backups)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from tools.backup_tiers import (
    AGES,
    ARCHIVE_RETENTION_FULL,
    BackupSet,
    parse_info,
    pitr_start,
    sets_to_expire,
    simulate,
    tier_for,
)


def at(d: date, hour: int = 1) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=UTC)


@pytest.mark.parametrize(
    ("today", "existing", "tier"),
    [
        # The first backup of the month is monthly, whatever the weekday (2026-11-01 is a Sunday).
        (date(2026, 11, 1), [], "monthly"),
        (date(2026, 11, 1), None, "monthly"),
        # The 1st's backup failed: the first one of the month is the 2nd's.
        (date(2026, 11, 2), [BackupSet("a", "monthly", at(date(2026, 10, 1)))], "monthly"),
        (date(2026, 11, 2), [BackupSet("a", "monthly", at(date(2026, 11, 1)))], "daily"),
        # Any other Sunday is weekly.
        (date(2026, 11, 8), [BackupSet("a", "monthly", at(date(2026, 11, 1)))], "weekly"),
        (date(2026, 11, 9), [BackupSet("a", "monthly", at(date(2026, 11, 1)))], "daily"),
        # Without the list, only the 1st counts as the month's first.
        (date(2026, 11, 15), None, "weekly"),
        (date(2026, 11, 17), None, "daily"),
        # A year boundary.
        (date(2027, 1, 1), [BackupSet("a", "monthly", at(date(2026, 12, 1)))], "monthly"),
    ],
)
def test_tier_for_calendar_cases(today, existing, tier) -> None:
    assert tier_for(today, existing) == tier


def test_sets_to_expire_by_tier_ages() -> None:
    now = at(date(2026, 12, 31), 12)
    ages = {"daily": 30, "weekly": 84, "monthly": 183}
    assert {k: v.days for k, v in AGES.items()} == ages
    sets = []
    for tier, days in ages.items():
        sets.append(BackupSet(f"{tier}-in", tier, now - timedelta(days=days - 1)))
        sets.append(BackupSet(f"{tier}-out", tier, now - timedelta(days=days + 1)))
    # Newer sets so that none of the above is protected as one of the newest.
    sets += [BackupSet(f"new-{i}", "daily", now - timedelta(hours=i + 1)) for i in range(ARCHIVE_RETENTION_FULL)]
    assert sorted(sets_to_expire(sets, now)) == ["daily-out", "monthly-out", "weekly-out"]


def test_unknown_tier_is_kept_as_long_as_a_monthly() -> None:
    now = at(date(2026, 12, 31), 12)
    sets = [BackupSet("manual", None, now - timedelta(days=100)), BackupSet("old", None, now - timedelta(days=200))]
    sets += [BackupSet(f"new-{i}", "daily", now - timedelta(hours=i + 1)) for i in range(ARCHIVE_RETENTION_FULL)]
    assert sets_to_expire(sets, now) == ["old"]


def test_never_expires_newest_full() -> None:
    now = at(date(2026, 12, 31), 12)
    only = [BackupSet("ancient", "daily", now - timedelta(days=400))]
    assert sets_to_expire(only, now) == []
    # Nothing ran for 40 days: the newest sets stay, however old.
    stale = [BackupSet(f"d{i}", "daily", now - timedelta(days=40 + i)) for i in range(20)]
    kept = {s.label for s in stale} - set(sets_to_expire(stale, now))
    assert "d0" in kept and len(kept) == ARCHIVE_RETENTION_FULL


def test_pitr_window_covers_7_days() -> None:
    """A year of daily backups, expired every day as the timer does: on every day, the WAL kept
    (from the ARCHIVE_RETENTION_FULL-th newest full on) reaches back at least 7 days, and the
    tiers keep what the spec says."""
    start = date(2026, 1, 1)
    days = [start + timedelta(days=i) for i in range(365)]
    history = simulate(days)
    for day, sets in history[ARCHIVE_RETENTION_FULL - 1:]:  # once a week of backups exists
        now = at(day, 3)
        assert now - pitr_start(sets) >= timedelta(days=7), day
    day, sets = history[-1]
    now = at(day, 3)
    by_tier = {t: [s for s in sets if s.tier == t] for t in ("daily", "weekly", "monthly")}
    assert all(now - s.stop_time <= AGES[s.tier] for s in sets if s not in sorted(sets, key=lambda s: s.stop_time)[-ARCHIVE_RETENTION_FULL:])
    # Each monthly is kept 183 days: on 31 December, August to December (July's just went).
    assert len(by_tier["monthly"]) >= 5
    assert len(by_tier["weekly"]) >= 10  # about 12 weeks of weeklies, minus the Sundays that were monthly
    assert 25 <= len(by_tier["daily"]) <= 31


def test_failed_days_still_cover_7_days() -> None:
    start = date(2026, 3, 1)
    days = [start + timedelta(days=i) for i in range(60) if i % 3 != 1]  # every third day fails
    for day, sets in simulate(days):
        assert at(day, 3) - pitr_start(sets) >= timedelta(days=7) or len(sets) < ARCHIVE_RETENTION_FULL


def test_parse_info_reads_labels_tiers_and_stop_times() -> None:
    info = [{
        "name": "agenda",
        "backup": [
            {"label": "20261101-010001F", "type": "full", "annotation": {"tier": "monthly"},
             "timestamp": {"start": 1793494801, "stop": 1793494861}, "info": {"repository": {"delta": 1234}}},
            {"label": "20261102-010001F", "type": "full", "timestamp": {"start": 1793581201, "stop": 1793581261}},
        ],
    }]
    sets = parse_info(info)
    assert sets == [
        BackupSet("20261101-010001F", "monthly", datetime.fromtimestamp(1793494861, UTC)),
        BackupSet("20261102-010001F", None, datetime.fromtimestamp(1793581261, UTC)),
    ]
