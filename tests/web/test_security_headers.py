"""Security review L2 (CSP), L3 (/healthz details for loopback only) and L5 (external links)."""

from __future__ import annotations

import json
import re
from datetime import date

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import Client

from agenda.models import Occurrence
from agenda_site.csp import ADMIN_POLICY, POLICY
from claims.keys import occurrence_key
from tests.helpers import dates_value, import_event

pytestmark = pytest.mark.usefixtures("small_geo")


@pytest.fixture
def occ() -> Occurrence:
    import_event("datatourisme:c", organizer={"name": "Comité", "url": "https://comite.example/"},
                 offers={"is_free": False, "price": "8 €", "url": "https://billets.example/"},
                 url="https://data.datatourisme.fr/abc", dates=[dates_value(date(2026, 11, 14), "21:00")])
    return Occurrence.objects.get(key=occurrence_key("datatourisme:c", date(2026, 11, 14)))


# --- L2: Content-Security-Policy ---------------------------------------------------------

def test_public_policy_is_strict() -> None:
    directives = dict(d.strip().split(" ", 1) for d in POLICY.split(";"))
    assert directives == {
        "default-src": "'none'", "script-src": "'self'", "style-src": "'self'", "img-src": "'self'",
        "connect-src": "'self'", "worker-src": "'self'", "manifest-src": "'self'", "form-action": "'self'",
        "frame-ancestors": "'none'", "base-uri": "'none'",
    }


@pytest.mark.parametrize("url", ["/", "/a-propos/", "/healthz", "/api/v1/categories", "/api/v1/nope",
                                 "/evenements/inconnu-zzzzzzzz-2026-11-05/"])
def test_public_responses_carry_the_policy(url: str) -> None:
    assert Client().get(url)["Content-Security-Policy"] == POLICY


def test_occurrence_page_carries_the_policy(occ: Occurrence) -> None:
    response = Client().get(occ.get_absolute_url())
    assert response.status_code == 200 and response["Content-Security-Policy"] == POLICY


def test_admin_renders_under_its_own_policy() -> None:
    client = Client()
    login = client.get("/admin/login/")
    assert login.status_code == 200 and login["Content-Security-Policy"] == ADMIN_POLICY
    staff = User.objects.create_superuser("staff", "staff@example.org", "x" * 20)
    client.force_login(staff)
    response = client.get("/admin/")
    assert response.status_code == 200
    assert response["Content-Security-Policy"] == ADMIN_POLICY
    html = response.content.decode()
    # The admin loads its scripts from files: the policy needs no 'unsafe-inline' for scripts.
    assert not re.search(r"<script(?![^>]*\bsrc=)(?![^>]*application/(ld\+)?json)[^>]*>", html)
    assert "script-src 'self';" in ADMIN_POLICY and "'unsafe-eval'" not in ADMIN_POLICY


# --- L3: /healthz details for loopback only ----------------------------------------------

@pytest.mark.parametrize("remote", ["127.0.0.1", "::1"])
def test_healthz_details_for_loopback(remote: str) -> None:
    response = Client(REMOTE_ADDR=remote).get("/healthz")
    assert response.status_code == 200
    assert json.loads(response.content) == {"status": "ok", "database": True, "migrations": True, "debug": False}


@pytest.mark.parametrize("remote", ["203.0.113.7", "", "10.0.0.1", "127.0.0.2"])
def test_healthz_status_only_for_everyone_else(remote: str) -> None:
    # Behind Caddy on a Unix socket, gunicorn sets REMOTE_ADDR to "" (no peer address).
    client = Client(REMOTE_ADDR=remote)
    response = client.get("/healthz")
    assert (response.status_code, json.loads(response.content)) == (200, {"status": "ok"})
    MigrationRecorder(connection).record_unapplied("agenda", "0001_initial")
    response = client.get("/healthz")
    assert (response.status_code, json.loads(response.content)) == (503, {"status": "unavailable"})
    assert response["Cache-Control"].startswith("max-age=0")


def test_forwarded_headers_dont_make_a_request_local() -> None:
    client = Client(REMOTE_ADDR="203.0.113.7")
    response = client.get("/healthz", HTTP_X_FORWARDED_FOR="127.0.0.1", HTTP_X_REAL_IP="127.0.0.1")
    assert json.loads(response.content) == {"status": "ok"}


# --- L5: external links -----------------------------------------------------------------

def test_external_links_are_nofollow_noopener_noreferrer_ugc(occ: Occurrence) -> None:
    html = Client().get(occ.get_absolute_url()).content.decode()
    links = re.findall(r'<a href="(https?://[^"]+)"([^>]*)>', html)
    assert {href for href, _ in links} == {"https://comite.example/", "https://billets.example/",
                                           "https://data.datatourisme.fr/abc"}
    for _, attrs in links:
        assert 'rel="nofollow noopener noreferrer ugc"' in attrs
