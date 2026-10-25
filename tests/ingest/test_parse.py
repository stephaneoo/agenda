from datetime import UTC, date, datetime

import pytest

from agenda.fields import MAX_NAME, problem
from agenda.periods import OccurrenceDates, expand
from ingest.datatourisme import mapping, parse
from tests.ingest.conftest import recorded_rows, row

SEEN = datetime(2026, 10, 26, 5, 0, tzinfo=UTC)
TODAY = date(2026, 10, 26)


def occurrences(raw: str | None) -> list[OccurrenceDates]:
    return expand(parse.periods(raw))


def test_periods_parsing_cases() -> None:
    # The simplified export writes every period as "<start date><-><end date>", joined by
    # "|": no times and no weekdays (checked on the whole dataset). A recurrence comes as a
    # list of single days.
    weekly = "2026-10-28<->2026-10-28|2026-10-14<->2026-10-14|2026-10-21<->2026-10-21"
    assert occurrences(weekly) == [
        OccurrenceDates(date(2026, 10, d), None, None, None) for d in (14, 21, 28)
    ]
    # A continuous range: one occurrence spanning it.
    assert occurrences("2026-09-26<->2026-11-08") == [
        OccurrenceDates(date(2026, 9, 26), None, date(2026, 11, 8), None)
    ]
    # A single day: a date only, no time, no end.
    assert occurrences("2026-11-13<->2026-11-13") == [OccurrenceDates(date(2026, 11, 13), None, None, None)]
    # An end before the start, in the same year: the export writes a season over the new
    # year with the start's year ("saison 2026-2027": 2026-10-23 to 2026-05-20).
    assert occurrences("2026-10-23<->2026-05-20") == [
        OccurrenceDates(date(2026, 10, 23), None, date(2027, 5, 20), None)
    ]
    # Ranges that touch or overlap are one run of days.
    assert occurrences("2026-11-28<->2026-12-18|2026-12-19<->2026-01-03") == [
        OccurrenceDates(date(2026, 11, 28), None, date(2027, 1, 3), None)
    ]
    assert occurrences("2026-07-01<->2026-08-31|2026-04-01<->2026-11-01") == [
        OccurrenceDates(date(2026, 4, 1), None, date(2026, 11, 1), None)
    ]
    # Malformed pieces are dropped, the rest kept; nothing at all gives no dates.
    assert occurrences("n'importe quoi|2026-02-30<->2026-03-01|1800-01-01<->1800-01-02|2026-11-13<->2026-11-13") == [
        OccurrenceDates(date(2026, 11, 13), None, None, None)
    ]
    assert occurrences("2026-11-13") == [OccurrenceDates(date(2026, 11, 13), None, None, None)]
    assert occurrences("") == occurrences(None) == []
    # Every recorded row parses into at least one date.
    for r in recorded_rows():
        assert occurrences(r["Periodes_regroupees"]), r["Periodes_regroupees"]


def test_category_mapping_covers_fixture_types_else_autre() -> None:
    seen = {t for r in recorded_rows() for t in parse.types(r["Categories_de_POI"])}
    # Every type of the recorded rows is either mapped or known to say nothing.
    assert seen <= set(mapping.TYPES) | mapping.GENERIC, seen - set(mapping.TYPES) - mapping.GENERIC
    assert mapping.category_for(["Market", "SaleEvent"]) == "marche"
    assert mapping.category_for(["GarageSale", "SaleEvent"]) == "brocante"
    assert mapping.category_for(["ScreeningEvent", "CulturalEvent"]) == "cinema"
    assert mapping.category_for(["Concert", "MusicEvent"]) == "concert"
    assert mapping.category_for(["TheaterEvent", "CulturalEvent"]) == "spectacle"
    assert mapping.category_for(["Exhibition", "CulturalEvent"]) == "exposition"
    assert mapping.category_for(["Conference", "CulturalEvent"]) == "atelier-conference"
    assert mapping.category_for(["Game", "CulturalEvent"]) == "loto-jeux"
    assert mapping.category_for(["LocalAnimation", "SocialEvent", "SportsEvent"]) == "fete"
    assert mapping.category_for(["Product", "SportsEvent", "Visit"]) == "sport-balade"
    # Lotos carry no type of their own: the name tells.
    assert mapping.category_for(["LocalAnimation", "SocialEvent"], "Loto de l'amicale") == "loto-jeux"
    assert mapping.category_for(["CulturalEvent", "EntertainmentAndEvent", "PointOfInterest", "Event"]) == "autre"
    assert mapping.category_for(["SomethingNew"]) == "autre"
    assert mapping.category_for([]) == "autre"
    for r in recorded_rows():
        assert mapping.category_for(parse.types(r["Categories_de_POI"]), r["Nom_du_POI"]) in mapping.SLUGS


def test_row_to_claims_truncates_and_writes_valid_claims() -> None:
    long_name = "Grande fête " + "des olives " * 40
    r = row(7, "2026-11-05<->2026-11-05|2026-11-12<->2026-11-12", name=long_name, Description="mot " * 3000)
    key, claims = parse.row_to_claims(r, commune="26220", commune_name="Nyons", seen_at=SEEN, today=TODAY)
    assert key == "datatourisme:00000000-0000-4000-8000-000000000007"
    by = {(c.target_key, c.field): c for c in claims}
    name = by[(key, "name")].value
    assert len(name) <= MAX_NAME and name.endswith("…") and name.startswith("Grande fête des olives")
    assert name[:-1] == name[:-1].rstrip() and long_name.startswith(name[:-1])  # cut between words
    assert len(by[(key, "description")].value) <= 10_000
    assert by[(key, "visibility")].value == {"value": "published"}
    assert by[(key, "source_url")].value == r["URI_ID_du_POI"]
    assert by[(key, "source_updated_at")].value == "2026-10-21"
    assert by[(key, "category")].value == "marche"
    assert by[(key, "location")].value == {
        "name": None, "street": "Centre ville", "postal_code": "26110", "commune": "26220",
        "commune_name": "Nyons", "lat": 44.360433, "lon": 5.139799,
    }
    occ = f"{key}/2026-11-12"
    assert by[(occ, "dates")].value == {"start_date": "2026-11-12", "start_time": None, "end_date": None, "end_time": None}
    assert by[(occ, "listed")].value is True
    assert {c.source for c in claims} == {"datatourisme"} and {c.seen_at for c in claims} == {SEEN}
    for c in claims:
        assert problem(c.target_type, c.target_key, c.field, c.value) is None, (c.field, c.value)


@pytest.mark.parametrize("bad", [{"URI_ID_du_POI": None}, {"Nom_du_POI": "  "}, {"Periodes_regroupees": "jamais"}])
def test_row_without_id_name_or_dates_is_refused(bad: dict) -> None:
    with pytest.raises(parse.RowError):
        parse.row_to_claims(row(1, **bad), commune="26220", commune_name="Nyons", seen_at=SEEN, today=TODAY)


# --- Weekday in the name (batch C review, M2a) -------------------------------------------------

@pytest.mark.parametrize("name, weekday", [
    ("Mardi : Marché hebdomadaire Grignan", 1),
    ("Marché nyonsais du jeudi", 3),
    ("Marché de Valréas - Tous les mardi", 1),
    ("Marché provençal tous les samedis", 5),
    ("Marché bio le VENDREDI", 4),
])
def test_long_period_with_one_weekday_in_the_name_expands_per_weekday(name: str, weekday: int) -> None:
    occs = expand(parse.periods("2026-01-01<->2026-12-31", name))
    assert 52 <= len(occs) <= 53
    assert {o.start_date.weekday() for o in occs} == {weekday}
    assert all(o.end_date is None and o.start_time is None for o in occs)


@pytest.mark.parametrize("name, periods", [
    ("Marché du mardi et du vendredi", "2026-01-01<->2026-12-31"),  # two weekdays
    ("Marché du jeudi", "2026-11-01<->2026-11-25"),  # 25 days: a short period stays one
    ("Exposition au château", "2026-01-01<->2026-12-31"),  # no weekday
    ("Les mardis de l'été et le jeudi", "2026-06-01<->2026-09-30"),  # two again
    ("Fête de la Saint-Samedi", "2026-01-01<->2026-12-31"),  # a weekday word, not a pattern
])
def test_no_weekday_expansion(name: str, periods: str) -> None:
    occs = expand(parse.periods(periods, name))
    assert len(occs) == 1


def test_row_to_claims_expands_a_weekly_market() -> None:
    r = row(5, "2026-01-01<->2026-12-31", name="Marché nyonsais du jeudi")
    key, claims = parse.row_to_claims(r, commune="26220", commune_name="Nyons", seen_at=SEEN, today=TODAY)
    dates = [c for c in claims if c.field == "dates"]
    assert len(dates) == 53  # 2026 has 53 Thursdays
    assert dates[0].target_key == f"{key}/2026-01-01" and dates[-1].target_key == f"{key}/2026-12-31"


# --- Category names (batch C review, minor 6) ----------------------------------------------------

@pytest.mark.parametrize("types, name, slug", [
    (["Product", "Visit", "CulturalEvent"], "Visite guidée du centre ancien de Dieulefit", "sport-balade"),
    (["Visit", "Product"], "Les Ateliers Créatifs de Myriam", "atelier-conference"),
    (["Visit", "CulturalEvent"], "Conférence sur la truffe", "atelier-conference"),
    (["Visit"], "Anniversaire de la commanderie", "fete"),
    (["Visit"], "Visite théâtralisée du château", "spectacle"),
    (["CulturalEvent"], "STAGE DE MISE EN SCÈNE", "atelier-conference"),
    (["CulturalEvent"], "Cours de poterie", "atelier-conference"),
    (["CulturalEvent"], 'Spectacle "Détective Lovecraft"', "spectacle"),
    (["CulturalEvent"], "Théâtre : Petits crimes conjugaux", "spectacle"),
    (["CulturalEvent"], "Une comédie de Molière", "spectacle"),
    (["CulturalEvent"], "Petits crimes conjugaux", "autre"),
    (["CulturalEvent"], "Tournoi de tarot", "autre"),  # tarot is not a loto
    (["Concert", "CulturalEvent"], "Stage et concert", "concert"),  # a specific type wins over the name
])
def test_category_names(types: list[str], name: str, slug: str) -> None:
    assert mapping.category_for(types, name) == slug
