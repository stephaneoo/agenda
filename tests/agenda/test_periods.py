from datetime import date, time

from agenda.periods import OccurrenceDates, Period, expand

TUESDAY = 1  # date.weekday()


def test_recurring_period_expands_per_date() -> None:
    nov = Period(date(2026, 11, 1), date(2026, 11, 30), time(8, 0), time(12, 30), weekdays=frozenset({TUESDAY}))
    assert expand([nov]) == [
        OccurrenceDates(date(2026, 11, d), time(8, 0), date(2026, 11, d), time(12, 30)) for d in (3, 10, 17, 24)
    ]


def test_continuous_range_is_one_spanning_occurrence() -> None:
    exhibition = Period(date(2026, 10, 1), date(2026, 12, 31))
    assert expand([exhibition]) == [OccurrenceDates(date(2026, 10, 1), None, date(2026, 12, 31), None)]
    festival = Period(date(2026, 11, 13), date(2026, 11, 15), time(18, 0), time(23, 0))
    assert expand([festival]) == [OccurrenceDates(date(2026, 11, 13), time(18, 0), date(2026, 11, 15), time(23, 0))]
    # A single day with no time: a date, and no end to display.
    assert expand([Period(date(2026, 11, 5), date(2026, 11, 5))]) == [OccurrenceDates(date(2026, 11, 5), None, None, None)]


def test_end_before_start_rolls_to_next_day() -> None:
    night = Period(date(2026, 11, 14), date(2026, 11, 14), time(19, 0), time(1, 0))
    assert expand([night]) == [OccurrenceDates(date(2026, 11, 14), time(19, 0), date(2026, 11, 15), time(1, 0))]
    fridays = Period(date(2026, 11, 1), date(2026, 11, 14), time(22, 0), time(2, 0), weekdays=frozenset({4}))
    assert [(o.start_date.day, o.end_date.day) for o in expand([fridays])] == [(6, 7), (13, 14)]


def test_two_showings_same_day_are_one_occurrence_with_earliest_start() -> None:
    evening = Period(date(2026, 11, 5), date(2026, 11, 5), time(20, 30))
    afternoon = Period(date(2026, 11, 5), date(2026, 11, 5), time(15, 0), time(17, 0))
    assert expand([evening, afternoon]) == [OccurrenceDates(date(2026, 11, 5), time(15, 0), date(2026, 11, 5), time(17, 0))]


def test_backwards_period_gives_nothing() -> None:
    assert expand([Period(date(2026, 11, 5), date(2026, 11, 1))]) == []


def test_claim_round_trip() -> None:
    o = OccurrenceDates(date(2026, 11, 14), time(19, 0), date(2026, 11, 15), time(1, 0))
    assert o.to_claim() == {"start_date": "2026-11-14", "start_time": "19:00", "end_date": "2026-11-15", "end_time": "01:00"}
    assert OccurrenceDates.from_claim(o.to_claim()) == o
    assert OccurrenceDates.from_claim({"start_date": "nope"}) is None
