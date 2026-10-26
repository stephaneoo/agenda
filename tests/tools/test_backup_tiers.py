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
        assert now - pitr_start(sets, now) >= timedelta(days=7), day
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
        assert at(day, 3) - pitr_start(sets, at(day, 3)) >= timedelta(days=7) or len(sets) < ARCHIVE_RETENTION_FULL


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


# --- Batch D review: Paris months, PITR by time ------------------------------------------------------

from zoneinfo import ZoneInfo  # noqa: E402

from tools.backup_tiers import archive_retention  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")


def paris(y, m, d, h=1, mi=30) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=PARIS).astimezone(UTC)


def test_the_month_is_the_paris_month_in_summer() -> None:
    # 2027-06-01 01:30 CEST is 2027-05-31 23:30 UTC: still June's monthly.
    june_first = BackupSet("a", "monthly", paris(2027, 6, 1))
    assert june_first.stop_time.month == 5
    assert tier_for(date(2027, 6, 2), [june_first]) == "daily"
    assert tier_for(date(2027, 6, 1), [BackupSet("b", "monthly", paris(2027, 5, 1))]) == "monthly"


def wal_reach(sets, now) -> timedelta:
    """How far back point-in-time recovery reaches: WAL is kept from the
    archive_retention(sets, now)-th newest set on."""
    ordered = sorted(sets, key=lambda s: s.stop_time)
    n = archive_retention(sets, now)
    return now - ordered[-min(n, len(ordered))].stop_time


@pytest.mark.parametrize("extra", [set(), {5, 6, 20, 21, 22, 40}])
def test_pitr_covers_7_days_across_dst_and_extra_fulls(extra) -> None:
    """Daily 01:30 Paris backups from 2027-03-10 to 2027-04-30 (DST starts 2027-03-28) and
    from 2027-10-10 to 2027-11-20 (it ends 2027-10-31), some days with extra full backups (a
    restore, a manual one): every day from the 8th on, WAL reaches back 7 days or more."""
    for start in (date(2027, 3, 10), date(2027, 10, 10)):
        days = [start + timedelta(days=i) for i in range(45)]
        for i, (day, sets) in enumerate(simulate(days, extra_days={days[j] for j in extra if j < len(days)})):
            if i < 8:
                continue
            now = paris(day.year, day.month, day.day, 3, 0)
            assert wal_reach(sets, now) >= timedelta(days=7), (day, len(sets))


def test_sets_within_8_days_are_never_expired() -> None:
    now = paris(2027, 4, 1, 3, 0)
    # Twenty backups in the last 8 days (restores), older ones beyond: none of the recent go.
    recent = [BackupSet(f"r{i}", "daily", now - timedelta(hours=9 * i + 1)) for i in range(20)]
    old = [BackupSet(f"o{i}", "daily", now - timedelta(days=40 + i)) for i in range(3)]
    gone = set(sets_to_expire(recent + old, now))
    assert not gone & {s.label for s in recent}
    assert archive_retention(recent + old, now) >= len([s for s in recent if now - s.stop_time <= timedelta(days=7)]) + 1
