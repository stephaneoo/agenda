import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import Client, override_settings

from agenda import read
from agenda.models import Occurrence
from claims.keys import occurrence_key
from claims.models import Source
from claims.services import NewClaim, write_claims
from tests.helpers import dates_value, import_event, location, moderate
from tests.web.jsonld_check import page_jsonld
from web import views
from web.jsonld import occurrence_jsonld

pytestmark = pytest.mark.usefixtures("small_geo")
TODAY = date(2026, 11, 5)  # a Thursday


@pytest.fixture(autouse=True)
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(views, "today", lambda: TODAY)
    monkeypatch.setattr(read, "today", lambda: TODAY)
    monkeypatch.setattr(views, "now", lambda: datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc))


@pytest.fixture
def client() -> Client:
    return Client()


def event(key: str, d: date, time: str | None = "10:00", **kw) -> Occurrence:
    import_event(key, dates=[dates_value(d, time)], **kw)
    return Occurrence.objects.get(key=occurrence_key(key, d))


def page(client: Client, occ: Occurrence):
    """An occurrence page; every one rendered here must carry valid JSON-LD."""
    response = client.get(occ.get_absolute_url())
    if response.status_code == 200:
        page_jsonld(response.content.decode())
    return response


def titles(html: str) -> list[str]:
    return re.findall(r'<a class="title"[^>]*>([^<]+)</a>', html)


def test_exactly_one_ldjson_script_in_initial_html(client: Client) -> None:
    occ = event("datatourisme:a", TODAY, name="Concert </script><b>rock</b>", category="concert")
    response = page(client, occ)
    html = response.content.decode()
    assert response.status_code == 200 and response["Content-Type"].startswith("text/html")
    data = page_jsonld(html)
    assert data == occurrence_jsonld(Occurrence.objects.select_related("event").get(pk=occ.pk))
    assert "<b>rock</b>" not in html  # escaped in the page, and in the JSON-LD


def test_agenda_defaults_nyons_30min_7days(client: Client) -> None:
    event("datatourisme:nyons", TODAY, name="Marché de Nyons")
    event("datatourisme:valreas", TODAY + timedelta(days=6), name="Loto à Valréas", loc=location("84138", "Valréas", "84600"))
    event("datatourisme:later", TODAY + timedelta(days=7), name="Trop tard")
    event("datatourisme:remuzat", TODAY, name="Hors zone", loc=location("26281", "Rémuzat", "26510"))
    response = client.get("/")
    html = response.content.decode()
    assert response.status_code == 200
    assert titles(html) == ["Marché de Nyons", "Loto à Valréas"]
    assert re.search(r'<option value="26220" selected>Nyons</option>', html)
    assert re.search(r'<option value="30" selected>', html)
    assert re.search(r'<option value="7j" selected>', html)
    assert '<html lang="fr">' in html


def test_filters_work_without_js(client: Client) -> None:
    event("datatourisme:nyons", TODAY, name="Marché de Nyons")
    event("datatourisme:mirabel", TODAY, "11:00", name="Concert à Mirabel", category="concert",
          loc=location("26182", "Mirabel-aux-Baronnies"))
    event("datatourisme:later", TODAY + timedelta(days=20), name="Dans trois semaines")
    html = lambda q: client.get("/?" + q).content.decode()  # noqa: E731
    assert titles(html("rayon=0")) == ["Marché de Nyons"]
    assert titles(html("commune=26182&rayon=0")) == ["Concert à Mirabel"]
    assert titles(html("categorie=concert")) == ["Concert à Mirabel"]
    assert titles(html("periode=30j")) == ["Marché de Nyons", "Concert à Mirabel", "Dans trois semaines"]
    assert titles(html("periode=aujourdhui&categorie=marche")) == ["Marché de Nyons"]
    # Values the form never sends fall back to the defaults instead of failing.
    assert client.get("/?commune=75056&rayon=999&periode=x&categorie=nope").status_code == 200


def test_spanning_in_en_cours_group_once(client: Client) -> None:
    import_event("datatourisme:expo", name="Exposition d'automne", category="exposition",
                 dates=[dates_value(date(2026, 10, 1), None, date(2026, 12, 31))])
    event("datatourisme:nyons", TODAY + timedelta(days=1), name="Marché de Nyons")
    html = client.get("/").content.decode()
    assert html.count("Exposition d&#x27;automne") == 1
    # The dated days come first; the long items after them, collapsed (no JS needed).
    assert html.index("vendredi 6 novembre") < html.index("Marché de Nyons") < html.index("En cours")
    assert re.search(r'<details class="ongoing">\s*<summary>En cours / toute la période \(1\)</summary>', html)
    assert html.index("En cours") < html.index("Exposition d&#x27;automne")


def test_footer_shows_last_sync(client: Client) -> None:
    assert "Dernière synchronisation : jamais" in client.get("/a-propos/").content.decode()
    when = datetime(2026, 11, 5, 5, 2, tzinfo=timezone.utc)
    Source.objects.filter(name="datatourisme").update(last_run_at=when, last_success_at=when, last_outcome="ok")
    event("datatourisme:a", TODAY)
    for url in ("/", "/a-propos/", Occurrence.objects.get().get_absolute_url()):
        assert "Dernière synchronisation : 5 novembre 2026 à 06:02" in client.get(url).content.decode()


def test_about_page_licences_charter_and_partial_run(client: Client) -> None:
    when = datetime(2026, 11, 5, 5, 2, tzinfo=timezone.utc)
    Source.objects.filter(name="datatourisme").update(last_run_at=when, last_success_at=when, last_outcome="partial")
    event("datatourisme:a", TODAY)
    html = client.get("/a-propos/").content.decode()
    assert "Licence Ouverte" in html and "AGPL" in html and "Charte éditoriale" in html
    assert "partielle" in html


def test_occurrence_page_cites_source_and_update_date(client: Client) -> None:
    occ = event("datatourisme:a", TODAY, url="https://data.datatourisme.fr/abc")
    html = page(client, occ).content.decode()
    assert re.search(r'Source : <a href="https://data.datatourisme.fr/abc"[^>]*>DATAtourisme</a>', html)
    assert "Licence Ouverte 2.0" in html and "mise à jour le 24 octobre 2026" in html


def test_past_occurrence_page_200_marked_over(client: Client) -> None:
    occ = event("datatourisme:past", date(2026, 10, 20))
    response = page(client, occ)
    assert response.status_code == 200
    assert "Cet événement est terminé" in response.content.decode()
    assert "Cet événement est terminé" not in page(client, event("datatourisme:future", TODAY + timedelta(days=1))).content.decode()


def test_hidden_410_moved_301(client: Client) -> None:
    hidden = event("datatourisme:h", TODAY)
    moderate("datatourisme:h", "event", "datatourisme:h", "visibility", {"value": "hidden", "reason": "x"})
    assert client.get(hidden.get_absolute_url()).status_code == 410

    old = event("datatourisme:m", TODAY + timedelta(days=1))
    new_key = occurrence_key("datatourisme:m", TODAY + timedelta(days=8))
    write_claims("datatourisme:m", [
        NewClaim("occurrence", new_key, "dates", dates_value(TODAY + timedelta(days=8), "10:00"), "moderation"),
        NewClaim("occurrence", new_key, "listed", True, "moderation"),
        NewClaim("occurrence", new_key, "status", "rescheduled", "moderation"),
        NewClaim("occurrence", new_key, "previous_start", old.start, "moderation"),
        NewClaim("occurrence", old.key, "moved_to", new_key, "moderation"),
    ])
    response = client.get(old.get_absolute_url())
    new = Occurrence.objects.get(key=new_key)
    assert (response.status_code, response["Location"]) == (301, new.get_absolute_url())
    assert page(client, new).status_code == 200

    assert client.get("/evenements/inconnu-zzzzzzzz-2026-11-05/").status_code == 404
    # A renamed event: the old slug redirects to the new one.
    import_event("datatourisme:m", name="Nouveau nom", dates=[dates_value(TODAY + timedelta(days=1), "10:00")])
    response = client.get(f"/evenements/ancien-nom-{new.event.url_key}-{new.local_date}/")
    assert response.status_code == 301 and "/evenements/nouveau-nom-" in response["Location"]


def test_healthz_503_with_unapplied_migration(client: Client) -> None:
    assert client.get("/healthz").status_code == 200
    MigrationRecorder(connection).record_unapplied("agenda", "0001_initial")
    response = client.get("/healthz")
    assert response.status_code == 503
    assert json.loads(response.content)["migrations"] is False


@pytest.mark.parametrize("debug", [False, True])
def test_healthz_reports_debug(client: Client, debug: bool) -> None:
    with override_settings(DEBUG=debug):
        response = client.get("/healthz")
    body = json.loads(response.content)
    assert response.status_code == 200 and response["Content-Type"] == "application/json"
    assert body == {"status": "ok", "database": True, "migrations": True, "debug": debug}


def test_rescheduled_page_shows_previous_date(client: Client) -> None:
    occ = event("datatourisme:r", TODAY + timedelta(days=2), "21:00")
    import_event("datatourisme:r", dates=[dates_value(TODAY + timedelta(days=2), "21:00", None, "01:00")])
    moderate("datatourisme:r", "occurrence", occ.key, "status", "rescheduled")
    moderate("datatourisme:r", "occurrence", occ.key, "previous_start", "2026-10-31T21:00:00+01:00")
    html = page(client, Occurrence.objects.get(pk=occ.pk)).content.decode()
    assert "Reprogrammé (initialement prévu le 31 octobre 2026 à 21:00)" in html
    assert "samedi 7 novembre 2026, 21:00 – dimanche 8 novembre 2026, 01:00" in html


# --- Review fixes ------------------------------------------------------------------------

def test_unsafe_urls_are_not_links(client: Client) -> None:
    occ = event("datatourisme:x", TODAY, url="javascript:alert(1)",
                organizer={"name": "Comité", "url": "JaVaScRiPt:alert(1)"},
                offers={"is_free": False, "price": "5 €", "url": "//evil.example/"})
    html = page(client, occ).content.decode()
    assert "javascript:" not in html.lower() and "evil.example" not in html
    data = page_jsonld(html)
    assert "url" not in data.get("organizer", {}) and "offers" not in data


def test_moved_chain_page_redirects_to_final(client: Client) -> None:
    key = "datatourisme:chain"
    days = [TODAY + timedelta(days=d) for d in (1, 8, 15)]
    import_event(key, dates=[dates_value(d, "10:00") for d in days])
    a, b, c = (Occurrence.objects.get(key=occurrence_key(key, d)) for d in days)
    moderate(key, "occurrence", a.key, "moved_to", b.key)
    moderate(key, "occurrence", b.key, "moved_to", c.key)
    response = client.get(a.get_absolute_url())
    assert (response.status_code, response["Location"]) == (301, c.get_absolute_url())
    moderate(key, "occurrence", c.key, "visibility", {"value": "hidden", "reason": "x"})
    assert client.get(a.get_absolute_url()).status_code == 410


@pytest.mark.parametrize("day", [date(2026, 10, 25), date(2027, 3, 28)])
def test_dst_day_rendering_and_en_cours(client: Client, monkeypatch: pytest.MonkeyPatch, day: date) -> None:
    monkeypatch.setattr(views, "today", lambda: day)
    import_event("datatourisme:day", name="Journée entière", dates=[dates_value(day)])  # 23 or 25 hours
    import_event("datatourisme:night", name="Nuit blanche", dates=[dates_value(day, "02:30")])
    eve = day - timedelta(days=1)
    # 06:00 to 06:30 the next day: 24 h 30 on the wall clock, 23 h 30 or 25 h 30 in UTC.
    import_event("datatourisme:long", name="Longue veille", dates=[dates_value(eve, "06:00", day, "06:30")])
    html = client.get("/").content.decode()
    heading = "dimanche 25 octobre" if day.month == 10 else "dimanche 28 mars"
    en_cours, day_heading = html.index("En cours"), html.index(heading)
    assert day_heading < html.index("Journée entière") < en_cours < html.index("Longue veille")
    assert day_heading < html.index("Nuit blanche") < en_cours
    expected = "02:30" if day.month == 10 else "03:00"  # 02:30 doesn't exist in March
    assert f'<span class="time">{expected}</span>' in html
    page(client, Occurrence.objects.get(key=occurrence_key("datatourisme:night", day)))


def test_truncated_list_says_so(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(3):
        event(f"datatourisme:{i}", TODAY, f"1{i}:00")
    assert "tronquée" not in client.get("/").content.decode()
    monkeypatch.setattr(views, "PAGE_SIZE", 1)
    monkeypatch.setattr(views, "MAX_PAGES", 2)
    html = client.get("/").content.decode()
    assert len(titles(html)) == 2
    assert "Liste tronquée" in html
