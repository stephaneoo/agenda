"""Batch C security review: hostile upstream rows and servers never stop or stall the sync."""

from __future__ import annotations

import gzip
import http.server
import json
import threading
import time
from datetime import date, timedelta
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.db import DataError, connections

from agenda.models import Event, Occurrence
from claims.models import Claim, SourceRecord
from ingest import sync
from ingest.datatourisme import client, parse
from ingest.models import SyncRun
from tests.ingest.conftest import NOW, FakeSession, pages_of, row

KEY = "datatourisme:00000000-0000-4000-8000-{:012d}"
TODAY = date(2026, 10, 26)  # NOW in Paris


def run(rows, **kw):
    return sync.run(session=FakeSession(pages_of(rows)), queries=[{}], **kw)


def synced(n: int) -> bool:
    return Event.objects.filter(key=KEY.format(n)).exists() and SourceRecord.objects.filter(
        external_id=f"00000000-0000-4000-8000-{n:012d}").exists()


# --- H1: one bad row never stops the run --------------------------------------------------------

@pytest.mark.usefixtures("real_geo")
@pytest.mark.parametrize("extra", [
    {"Contacts_du_POI": "tel\u0000 04 75"},  # a column that is not a claim: only in the payload
    {"Nom_du_POI": "Marché\u0000 du soir"},
    {"Description": "d\ud800 lone surrogate"},
    {"Contacts_du_POI": "x\udfff", "Clé\u0000": "v"},  # a key too
])
def test_nul_and_lone_surrogates_are_sanitised_and_the_next_rows_sync(clock, extra) -> None:
    result = run([row(1), row(2, **extra), row(3)])
    assert result.status == "clean" and result.added == 3 and result.refused_rows == 0
    assert all(synced(n) for n in (1, 2, 3))
    payload = SourceRecord.objects.get(external_id="00000000-0000-4000-8000-000000000002").payload
    text = json.dumps(payload)
    assert "\\u0000" not in text and "\\ud800" not in text and "\\udfff" not in text
    name = Event.objects.get(key=KEY.format(2)).name
    assert "\x00" not in name


def test_sanitise_is_recursive_over_keys_and_values() -> None:
    raw = {"a\x00": ["b\ud800", {"c": "\udc00d\x00"}], "n": 1, "ok": "😀"}
    assert client.sanitise(raw) == {"a": ["b�", {"c": "�d"}], "n": 1, "ok": "😀"}


@pytest.mark.usefixtures("real_geo")
def test_a_database_error_on_one_row_is_that_row_refused(clock) -> None:
    real = sync._store_record

    def fail_two(record, ext_id, *args):
        if ext_id.endswith("000000000002"):
            raise DataError("unsupported Unicode escape sequence")
        return real(record, ext_id, *args)

    real_write = sync.write_claims

    def encode_error_three(key, claims):
        if key == KEY.format(3):
            raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")
        return real_write(key, claims)

    with mock.patch.object(sync, "_store_record", fail_two), mock.patch.object(sync, "write_claims", encode_error_three):
        result = run([row(1), row(2), row(3), row(4)])
    assert result.status == "partial" and result.refused_rows == 2
    assert synced(1) and synced(4) and not synced(3)


# --- M3, L4, L5: what a row may become ---------------------------------------------------------

def claims_of(r: dict) -> list:
    return parse.row_to_claims(r, commune="26220", commune_name="Nyons", seen_at=NOW, today=TODAY)[1]


def dates_of(r: dict) -> list[str]:
    return sorted(c.value["start_date"] for c in claims_of(r) if c.field == "dates")


def test_a_weekly_range_over_centuries_is_cut_to_the_window() -> None:
    days = dates_of(row(1, "1912-01-01<->2200-12-31", name="Marché du jeudi"))
    lo, hi = parse.window(TODAY)
    assert (lo, hi) == (date(2025, 10, 26), date(2028, 10, 26))
    assert 150 < len(days) <= 160
    assert days[0] >= lo.isoformat() and days[-1] <= hi.isoformat()
    assert all(date.fromisoformat(d).weekday() == 3 for d in days)


def test_a_continuous_range_overlapping_the_window_is_kept_whole() -> None:
    (dates,) = [c.value for c in claims_of(row(1, "2025-06-01<->2027-12-31")) if c.field == "dates"]
    assert dates["start_date"] == "2025-06-01" and dates["end_date"] == "2027-12-31"  # its key never moves


def test_more_than_400_dates_is_refused() -> None:
    many = "|".join(f"{TODAY + timedelta(days=i)}<->{TODAY + timedelta(days=i)}" for i in range(1000))
    with pytest.raises(parse.TooManyDates):
        claims_of(row(1, many))
    assert len(dates_of(row(1, "|".join(many.split("|")[:400])))) == 400


def test_a_period_entirely_outside_the_window_has_no_dates() -> None:
    with pytest.raises(parse.RowError, match="no usable dates"):
        claims_of(row(1, "2030-01-01<->2030-01-01|1990-05-01<->1990-06-30"))


@pytest.mark.usefixtures("real_geo")
def test_too_many_dates_refuses_the_row_and_says_so(clock) -> None:
    many = "|".join(f"{TODAY + timedelta(days=i)}<->{TODAY + timedelta(days=i)}" for i in range(1000))
    started = time.monotonic()
    result = run([row(1), row(2, many), row(3)])
    assert time.monotonic() - started < 30
    assert result.status == "partial" and result.too_many_dates == 1 and result.failed_rows == 0
    assert "1 refused for too many dates" in result.summary()
    assert synced(1) and synced(3) and not Event.objects.filter(key=KEY.format(2)).exists()


def test_clean_strips_bidi_and_format_characters() -> None:
    assert parse.clean("Ma‮rch‬⁦é⁩ ‏du‎ jeudi؜​﻿") == "Marché du jeudi"
    assert parse.clean("a\x85b\x9fc", one_line=True) == "a b c"
    (name,) = [c.value for c in claims_of(row(1, name="‮Marché")) if c.field == "name"]
    assert name == "Marché"


@pytest.mark.parametrize("uri, expected", [
    ("https://data.datatourisme.fr/13/abc", "https://data.datatourisme.fr/13/abc"),
    ("http://data.datatourisme.fr/13/abc", None),
    ("https://data.datatourisme.fr.evil.example/13/abc", None),
    ("https://evil.example/data.datatourisme.fr/abc", None),
])
def test_source_url_is_a_datatourisme_https_url_or_null(uri, expected) -> None:
    (value,) = [c.value for c in claims_of(row(1, URI_ID_du_POI=uri)) if c.field == "source_url"]
    assert value == expected


def test_parser_version_bumped() -> None:
    assert sync.PARSER_VERSION >= 3


# --- M1, M2, L1: the HTTP client ---------------------------------------------------------------

BOMB = gzip.compress(b" " * (64 * 1024 * 1024), 9)  # 64 MiB of spaces, ~64 KiB compressed


class Upstream(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self) -> None:
        Upstream.hits.append(self.path)
        port = self.server.server_port
        if self.path == "/bomb":
            self._send(200, BOMB, {"Content-Encoding": "gzip"})
        elif self.path == "/announced-huge":
            self.send_response(200)
            self.send_header("Content-Length", str(10 ** 10))
            self.end_headers()
        elif self.path == "/drip":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass
        elif self.path == "/to-other-host":
            self._send(302, b"", {"Location": f"http://127.0.0.2:{port}/landed"})
        elif self.path == "/to-http":
            self._send(301, b"", {"Location": f"http://localhost:{port}/landed"})
        elif self.path == "/busy":
            if Upstream.hits.count("/busy") == 1:
                self._send(503, b"", {"Retry-After": "99999999"})
            else:
                self._send(200, b'{"ok": true}', {})
        else:
            self._send(200, b'{"landed": true}', {})

    def _send(self, status: int, body: bytes, headers: dict) -> None:
        self.send_response(status)
        for k, v in {"Content-Length": str(len(body)), **headers}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def upstream():
    Upstream.hits = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def local_session():
    return client.make_session(hosts={"127.0.0.1"}, schemes=("http",))


def test_a_gzip_bomb_stops_at_the_cap(upstream) -> None:
    with pytest.raises(client.FetchError, match="over"):
        client._get_json(local_session(), f"{upstream}/bomb", None, "bomb")


def test_a_content_length_over_the_cap_is_refused_unread(upstream) -> None:
    with pytest.raises(client.FetchError, match="Content-Length"):
        client._get_json(local_session(), f"{upstream}/announced-huge", None, "huge")


def test_a_slow_drip_fails_at_the_deadline(upstream) -> None:
    started = time.monotonic()
    with pytest.raises(client.FetchError, match="deadline"):
        client._get_json(local_session(), f"{upstream}/drip", None, "drip", deadline=started + 1)
    assert time.monotonic() - started < 5


def test_retry_after_is_capped(upstream, monkeypatch) -> None:
    retry = client.make_session().adapters["https://"].max_retries
    assert retry.parse_retry_after("99999999") == client.MAX_RETRY_AFTER == 60
    assert retry.parse_retry_after("Fri, 31 Dec 2100 23:59:59 GMT") == 60
    assert retry.parse_retry_after("5") == 5
    monkeypatch.setattr(client, "MAX_RETRY_AFTER", 0.5)
    started = time.monotonic()
    assert client._get_json(local_session(), f"{upstream}/busy", None, "busy") == {"ok": True}
    assert time.monotonic() - started < 5 and Upstream.hits.count("/busy") == 2


@pytest.mark.parametrize("path", ["/to-other-host", "/to-http"])
def test_redirects_are_never_followed(upstream, path) -> None:
    with pytest.raises(client.FetchError, match="redirect"):
        client._get_json(local_session(), f"{upstream}{path}", None, "r")
    assert "/landed" not in Upstream.hits


def test_the_live_session_refuses_http_and_other_hosts_before_connecting(upstream) -> None:
    session = client.make_session()
    with pytest.raises(client.FetchError, match="not allowed"):
        client._get_json(session, f"{upstream}/landed", None, "plain http")
    with pytest.raises(client.FetchError, match="not allowed"):
        client._get_json(session, "https://evil.example/data/", None, "other host")
    with pytest.raises(client.FetchError, match="not allowed"):
        client._get_json(session, "https://tabular-api.data.gouv.fr.evil.example/", None, "lookalike")
    assert Upstream.hits == []
    assert client.ALLOWED_HOSTS == {"tabular-api.data.gouv.fr", "www.data.gouv.fr"}
    assert all(u.startswith("https://") and u.split("/")[2] in client.ALLOWED_HOSTS
               for u in (client.URL, client.RESOURCE_META_URL, client.TABULAR_META_URL))


class DeepSession:
    def get(self, *args, **kwargs):
        from tests.ingest.conftest import FakeRaw

        response = mock.Mock(status_code=200, headers={})
        response.raw = FakeRaw(b"[" * 100_000 + b"]" * 100_000)
        return response


def test_json_nested_too_deep_is_a_fetch_error() -> None:
    with pytest.raises(client.FetchError, match="not JSON"):
        client._get_json(DeepSession(), client.URL, None, "deep")


@pytest.mark.usefixtures("real_geo")
def test_a_run_past_its_deadline_fails_and_withdraws_nothing(clock) -> None:
    run([row(1), row(2)])
    clock.now = NOW + timedelta(days=1)
    result = run([row(1)], deadline_seconds=-1)
    assert result.status == "failed" and "deadline" in result.error and result.withdrawn == 0
    assert Occurrence.objects.get(key=f"{KEY.format(2)}/2026-11-05").listed


# --- L7: --allow-mass-withdrawal=N ------------------------------------------------------------

@pytest.mark.usefixtures("real_geo")
def test_allow_mass_withdrawal_needs_the_exact_count(clock) -> None:
    rows = [row(i, "2026-11-10<->2026-11-10") for i in range(10)]
    run(rows)
    clock.now = NOW + timedelta(days=1)
    assert run(rows[:5]).skipped_withdrawals == 5
    assert sync.skipped_last_partial() == 5
    wrong = run(rows[:5], allow_mass_withdrawal=4)
    assert wrong.status == "partial" and wrong.withdrawn == 0 and "refused" in wrong.warning
    nothing = run([], allow_mass_withdrawal=10)  # the export came back empty: never
    assert nothing.status == "partial" and nothing.withdrawn == 0 and nothing.fetched_rows == 0
    assert run(rows[:5], allow_mass_withdrawal=5).withdrawn == 5


@pytest.mark.django_db
def test_the_command_checks_the_count_against_the_last_partial_run() -> None:
    with pytest.raises(CommandError, match="skipped no withdrawals"):
        call_command("sync_datatourisme", "--allow-mass-withdrawal=3")
    SyncRun.objects.create(started_at=NOW, status="partial", skipped_withdrawals=7)
    with pytest.raises(CommandError, match="skipped 7"):
        call_command("sync_datatourisme", "--allow-mass-withdrawal=3")
    with mock.patch.object(sync, "run", return_value=SyncRun(started_at=NOW, status="clean")) as fake:
        call_command("sync_datatourisme", "--allow-mass-withdrawal=7")
    fake.assert_called_once_with(allow_mass_withdrawal=7)
    with pytest.raises(CommandError):
        call_command("sync_datatourisme", "--allow-mass-withdrawal")  # a count is required


# --- L8: the run lock's key space ----------------------------------------------------------------

@pytest.mark.usefixtures("real_geo")
def test_the_run_lock_is_two_int4_keys_apart_from_the_per_event_bigint_locks(clock) -> None:
    other = connections.create_connection("default")
    try:
        with other.cursor() as cursor:
            # The same 64 bits in the one-bigint form, as write_claims' event locks take them.
            cursor.execute("SELECT pg_advisory_lock(%s)", [(sync.LOCK_CLASS << 32) | sync.LOCK_OBJECT])
        assert run([row(1)]).status == "clean"
    finally:
        other.close()
    assert Claim.objects.filter(event_key=KEY.format(1)).exists()
