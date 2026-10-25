from datetime import date, datetime, timedelta, timezone

import pytest

from agenda import read
from agenda.models import Occurrence
from agenda.read import Gone, InvalidQuery, MovedTo, NotFound, search_occurrences
from claims.keys import occurrence_key
from claims.models import Source
from claims.services import NewClaim, write_claims
from tests.helpers import T0, dates_value, import_event, location, moderate

pytestmark = pytest.mark.usefixtures("small_geo")

NOV1 = date(2026, 11, 1)


def search(**kw):
    args = {"commune": None, "within": None, "date_from": NOV1, "date_to": NOV1 + timedelta(days=30),
            "categories": [], "cursor": None} | kw
    return search_occurrences(**args)


def keys(page) -> list[str]:
    return [o.key for o in page.items]


def one(key: str, d: date, time: str | None = None, **kw) -> str:
    import_event(key, dates=[dates_value(d, time)], **kw)
    return occurrence_key(key, d)


@pytest.mark.parametrize("kw, detail", [
    ({"within": 30}, "commune"),
    ({"commune": "26220", "within": 61}, "within"),
    ({"commune": "26220", "within": -1}, "within"),
    ({"commune": "75056", "within": 10}, "covered"),
    ({"date_from": date(2026, 11, 10), "date_to": date(2026, 11, 1)}, "before"),
    ({"date_from": date(2026, 11, 1), "date_to": date(2027, 3, 1)}, "92"),
    ({"categories": ["marche", "inconnue"]}, "category"),
    ({"cursor": "pas-un-curseur"}, "cursor"),
], ids=["within-without-commune", "within-61", "within-negative", "commune-75056", "to-before-from",
        "span-over-92", "unknown-category", "bad-cursor"])
def test_invalid_requests(kw: dict, detail: str) -> None:
    with pytest.raises(InvalidQuery) as e:
        search(**kw)
    assert detail in e.value.detail


def test_span_of_92_days_is_allowed() -> None:
    assert search(date_from=NOV1, date_to=NOV1 + timedelta(days=92)).items == []


def test_limit_over_200_rejected() -> None:
    with pytest.raises(InvalidQuery):
        search(limit=201)
    with pytest.raises(InvalidQuery):
        search(limit=0)
    assert search(limit=200).next is None


def test_defaults_today_plus_30(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read, "today", lambda: NOV1)
    first = one("datatourisme:a", NOV1)
    last = one("datatourisme:b", date(2026, 12, 1), "23:30")
    one("datatourisme:c", date(2026, 12, 2))
    one("datatourisme:d", date(2026, 10, 31), "23:00")
    page = search_occurrences(None, None, None, None, [], None)
    assert keys(page) == [first, last]


def test_overlap_spanning_and_date_only() -> None:
    day = date(2026, 11, 5)
    import_event("datatourisme:expo", dates=[dates_value(date(2026, 10, 1), None, date(2026, 12, 31))])
    import_event("datatourisme:until4", dates=[dates_value(date(2026, 11, 1), None, date(2026, 11, 4))])
    import_event("datatourisme:night", dates=[dates_value(date(2026, 11, 4), "21:00", date(2026, 11, 5), "01:00")])
    import_event("datatourisme:midnight", dates=[dates_value(date(2026, 11, 4), "21:00", date(2026, 11, 5), "00:00")])
    import_event("datatourisme:dateonly", dates=[dates_value(day)])
    import_event("datatourisme:late", dates=[dates_value(day, "23:59")])
    import_event("datatourisme:next", dates=[dates_value(date(2026, 11, 6), "00:00")])
    found = {k.split("/")[0].split(":")[1] for k in keys(search(date_from=day, date_to=day))}
    assert found == {"expo", "night", "dateonly", "late"}


def test_sorted_by_start_then_id() -> None:
    one("datatourisme:z", date(2026, 11, 3), "10:00")
    one("datatourisme:y", date(2026, 11, 2), "18:00")
    same = [one(f"datatourisme:same{i}", date(2026, 11, 2), "09:00") for i in range(5)]
    one("datatourisme:x", date(2026, 11, 2))  # a date starts at 00:00
    items = search().items
    assert [o.start_utc for o in items] == sorted(o.start_utc for o in items)
    tied = [o.id for o in items if o.key in same]
    assert tied == sorted(tied)
    # Walking the ties one per page gives each exactly once, in order.
    seen, cursor = [], None
    while True:
        page = search(limit=1, cursor=cursor)
        seen += [o.id for o in page.items]
        if not (cursor := page.next):
            break
    assert seen == [o.id for o in items]


def test_hidden_excluded_cancelled_included() -> None:
    hidden = one("datatourisme:hidden", date(2026, 11, 2))
    moderate("datatourisme:hidden", "event", "datatourisme:hidden", "visibility", {"value": "hidden", "reason": "x"})
    occ_hidden = one("datatourisme:occhidden", date(2026, 11, 2))
    moderate("datatourisme:occhidden", "occurrence", occ_hidden, "visibility", {"value": "hidden", "reason": "x"})
    pending = one("datatourisme:pending", date(2026, 11, 2), visibility={"value": "pending"})
    cancelled = one("datatourisme:cancelled", date(2026, 11, 3))
    moderate("datatourisme:cancelled", "occurrence", cancelled, "status", "cancelled")
    postponed = one("datatourisme:postponed", date(2026, 11, 4))
    moderate("datatourisme:postponed", "occurrence", postponed, "status", "postponed")
    found = keys(search())
    assert found == [cancelled, postponed]
    assert hidden not in found and occ_hidden not in found and pending not in found


def test_radius_and_category_filters() -> None:
    nyons = one("datatourisme:n", date(2026, 11, 2), "10:00")
    mirabel = one("datatourisme:m", date(2026, 11, 2), "11:00", loc=location("26182", "Mirabel-aux-Baronnies"), category="concert")
    valreas = one("datatourisme:v", date(2026, 11, 2), "12:00", loc=location("84138", "Valréas", "84600"))
    assert keys(search(commune="26220", within=0)) == [nyons]
    assert keys(search(commune="26220", within=10)) == [nyons, mirabel]
    assert keys(search(commune="26220", within=30)) == [nyons, mirabel, valreas]
    assert keys(search(commune="84138")) == [valreas]  # a commune alone: that commune only
    assert keys(search(categories=["concert"])) == [mirabel]
    assert keys(search(categories=["concert", "marche"])) == [nyons, mirabel, valreas]


def test_past_occurrence_only_when_range_includes_it() -> None:
    past = one("datatourisme:past", date(2026, 10, 20), "10:00")
    assert past not in keys(search())
    assert keys(search(date_from=date(2026, 10, 20), date_to=date(2026, 10, 20))) == [past]
    assert isinstance(read.get_occurrence(Occurrence.objects.get(key=past).id), Occurrence)


def test_cursor_stable_under_concurrent_projection() -> None:
    k = {c: one(f"datatourisme:{c}", date(2026, 11, 2 + i), "10:00") for i, c in enumerate("abcdef")}
    first = search(limit=3)
    assert keys(first) == [k["a"], k["b"], k["c"]] and first.next
    # Between the two pages, the projection changes:
    import_event("datatourisme:d", name="Renommé", dates=[dates_value(date(2026, 11, 5), "10:00")], seen_at=T0 + timedelta(hours=1))
    moderate("datatourisme:b", "event", "datatourisme:b", "visibility", {"value": "hidden", "reason": "x"})
    behind = one("datatourisme:behind", date(2026, 11, 3), "12:00")  # sorts before the cursor
    ahead = one("datatourisme:ahead", date(2026, 11, 6), "12:00")
    write_claims("datatourisme:e", [NewClaim("occurrence", k["e"], "listed", False, "datatourisme", T0 + timedelta(hours=2))])
    second = search(limit=50, cursor=first.next)
    assert keys(second) == [k["d"], ahead, k["f"]]
    assert second.next is None
    assert second.items[0].event.name == "Renommé"
    assert not set(keys(first)) & set(keys(second))
    assert behind not in keys(second)


def test_get_occurrence_outcomes() -> None:
    a = one("datatourisme:a", date(2026, 11, 5))
    one("datatourisme:h", date(2026, 11, 5))
    moderate("datatourisme:h", "event", "datatourisme:h", "visibility", {"value": "hidden", "reason": "x"})
    new = occurrence_key("datatourisme:a", date(2026, 11, 26))
    write_claims("datatourisme:a", [
        NewClaim("occurrence", new, "dates", dates_value(date(2026, 11, 26)), "moderation"),
        NewClaim("occurrence", new, "listed", True, "moderation"),
        NewClaim("occurrence", new, "status", "rescheduled", "moderation"),
        NewClaim("occurrence", new, "previous_start", "2026-11-05", "moderation"),
        NewClaim("occurrence", a, "moved_to", new, "moderation"),
    ])
    get = lambda key: read.get_occurrence(Occurrence.objects.get(key=key).id)  # noqa: E731
    assert get(a) == MovedTo(Occurrence.objects.get(key=new).id)
    assert isinstance(get(new), Occurrence)
    assert isinstance(get(occurrence_key("datatourisme:h", date(2026, 11, 5))), Gone)
    with pytest.raises(NotFound):
        read.get_occurrence("occ_nope")


def test_get_event_with_future_published_occurrences(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read, "today", lambda: NOV1)
    import_event("datatourisme:m", dates=[dates_value(d) for d in (date(2026, 10, 29), NOV1, date(2026, 11, 8), date(2026, 11, 15))])
    moderate("datatourisme:m", "occurrence", occurrence_key("datatourisme:m", date(2026, 11, 8)), "visibility", {"value": "hidden", "reason": "x"})
    event = Occurrence.objects.first().event
    got = read.get_event(event.id)
    assert [o.local_date for o in got.future_occurrences] == [NOV1, date(2026, 11, 15)]
    with pytest.raises(NotFound):
        read.get_event("evt_nope")
    moderate("datatourisme:m", "event", "datatourisme:m", "visibility", {"value": "hidden", "reason": "x"})
    assert isinstance(read.get_event(event.id), Gone)


def test_occurrence_dict_shape() -> None:
    key = one("datatourisme:m", date(2026, 11, 5))
    data = read.occurrence_dict(Occurrence.objects.get(key=key), "https://example.org")
    assert list(data) == [
        "id", "url", "event_id", "name", "description", "start", "end", "timezone", "status", "previous_start",
        "category", "location", "organizer", "offers", "sources", "updated_at",
    ]
    assert data["url"].startswith("https://example.org/evenements/marche-de-nyons-") and data["url"].endswith("-2026-11-05/")
    assert (data["start"], data["end"], data["timezone"], data["status"]) == ("2026-11-05", None, "Europe/Paris", "scheduled")
    assert data["sources"][0]["updated_at"] == "2026-10-24T05:00:00+02:00"


def test_sync_status() -> None:
    assert read.sync_status() == {"last_sync": None, "sources": []}
    one("datatourisme:a", date(2026, 11, 5))
    when = datetime(2026, 10, 25, 4, 2, 11, tzinfo=timezone.utc)
    Source.objects.filter(name="datatourisme").update(last_run_at=when, last_outcome="ok", last_success_at=when)
    assert read.sync_status() == {
        "last_sync": when,
        "sources": [{"name": "DATAtourisme", "last_success": when, "last_run": when, "last_outcome": "ok"}],
    }


def _chain(final_visible: bool = True) -> tuple[str, str, str]:
    """A -> B -> C, moved by a moderator; C hidden unless final_visible."""
    key = "datatourisme:chain"
    days = [date(2026, 11, d) for d in (5, 12, 19)]
    import_event(key, dates=[dates_value(d, "10:00") for d in days])
    a, b, c = (occurrence_key(key, d) for d in days)
    moderate(key, "occurrence", a, "moved_to", b)
    moderate(key, "occurrence", b, "moved_to", c)
    if not final_visible:
        moderate(key, "occurrence", c, "visibility", {"value": "hidden", "reason": "x"})
    return tuple(Occurrence.objects.get(key=k).id for k in (a, b, c))


def test_moved_chain_goes_straight_to_final_target() -> None:
    a, b, c = _chain()
    assert read.get_occurrence(a) == MovedTo(c)
    assert read.get_occurrence(b) == MovedTo(c)


def test_moved_chain_to_hidden_target_is_gone() -> None:
    a, b, c = _chain(final_visible=False)
    assert isinstance(read.get_occurrence(a), Gone)
    assert isinstance(read.get_occurrence(c), Gone)


def test_default_from_is_today_in_paris(monkeypatch: pytest.MonkeyPatch) -> None:
    from django.utils import timezone as dj_timezone
    one("datatourisme:oct31", date(2026, 10, 31), "18:00")
    nov1 = one("datatourisme:nov1", date(2026, 11, 1), "10:00")
    # 23:30 UTC on 31 October is already 00:30 on 1 November in Paris.
    monkeypatch.setattr(dj_timezone, "now", lambda: datetime(2026, 10, 31, 23, 30, tzinfo=timezone.utc))
    assert keys(search_occurrences(None, None, None, None, [], None)) == [nov1]
