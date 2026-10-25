from datetime import date, datetime, time, timedelta, timezone

from agenda.timeutil import bounds, format_local, to_local


def test_dst_ambiguous_and_nonexistent() -> None:
    # 2026-10-25: 02:30 happens twice; the first instance (summer time, +02:00) is taken.
    assert to_local(date(2026, 10, 25), time(2, 30)) == "2026-10-25T02:30:00+02:00"
    assert to_local(date(2026, 10, 25), time(3, 30)) == "2026-10-25T03:30:00+01:00"
    # The last Sunday of March: 02:30 doesn't exist, and moves forward to 03:00 +02:00.
    assert to_local(date(2026, 3, 29), time(2, 30)) == "2026-03-29T03:00:00+02:00"
    assert to_local(date(2027, 3, 28), time(2, 30)) == "2027-03-28T03:00:00+02:00"
    assert to_local(date(2027, 3, 28), time(2, 0)) == "2027-03-28T03:00:00+02:00"
    assert to_local(date(2027, 3, 28), time(1, 59)) == "2027-03-28T01:59:00+01:00"


def test_to_local_offsets_and_bare_dates() -> None:
    assert to_local(date(2026, 11, 7), time(21, 0)) == "2026-11-07T21:00:00+01:00"
    assert to_local(date(2026, 7, 14), time(22, 0)) == "2026-07-14T22:00:00+02:00"
    assert to_local(date(2026, 11, 5), None) == "2026-11-05"


def test_format_local() -> None:
    assert format_local(datetime(2026, 10, 25, 4, 2, 11, 500, tzinfo=timezone.utc)) == "2026-10-25T05:02:11+01:00"


def test_bounds_date_only_end_is_inclusive_to_midnight() -> None:
    start, end = bounds("2026-10-01", "2026-12-31")
    assert start == datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)
    assert end + timedelta(microseconds=1) == datetime(2026, 12, 31, 23, 0, tzinfo=timezone.utc)  # Jan 1st 00:00 local
    # No end: a date lasts the day, a time is an instant.
    assert bounds("2026-11-05", None)[1] == datetime(2026, 11, 5, 22, 59, 59, 999999, tzinfo=timezone.utc)
    s, e = bounds("2026-11-14T21:00:00+01:00", None)
    assert s == e == datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc)
