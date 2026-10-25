"""Batch C security review: the service worker's kill switch, origin and loopback rules."""

import re

import pytest
from django.test import Client

from web import views


@pytest.fixture(autouse=True)
def fresh_worker():
    views._cached_service_worker.cache_clear()
    yield
    views._cached_service_worker.cache_clear()


def test_the_worker_is_live_by_default(client, db) -> None:
    body = client.get("/sw.js").content.decode()
    assert "const KILL = false;" in body
    assert 'name="agenda-sw"' not in client.get("/a-propos/").content.decode()


def test_the_kill_switch_serves_a_worker_that_removes_itself(client, db, settings) -> None:
    settings.SW_KILL = True
    body = client.get("/sw.js").content.decode()
    assert "const KILL = true;" in body
    assert "registration.unregister()" in body and "caches.delete" in body
    assert '<meta name="agenda-sw" content="kill">' in client.get("/a-propos/").content.decode()


def test_production_never_serves_the_worker_to_loopback(db, settings) -> None:
    settings.PRODUCTION = True
    for remote in ("127.0.0.1", "::1"):
        assert Client(REMOTE_ADDR=remote).get("/sw.js").status_code == 404
    assert Client(REMOTE_ADDR="").get("/sw.js").status_code == 200  # through Caddy's socket
    settings.PRODUCTION = False
    assert Client(REMOTE_ADDR="127.0.0.1").get("/sw.js").status_code == 200  # dev and tests


def origin_meta(html: str) -> str | None:
    m = re.search(r'<meta name="agenda-origin" content="([^"]*)">', html)
    return m[1] if m else None


@pytest.mark.parametrize("site_url, dev, expected", [
    ("https://agenda.example.org", False, "https://agenda.example.org"),
    ("https://agenda.example.org/sous/chemin", True, "https://agenda.example.org"),
    ("", True, "http://testserver"),  # dev, unset: the current origin
    ("", False, ""),  # production always sets it; unset, nowhere to register
])
def test_the_page_names_the_origin_the_worker_may_register_on(client, db, settings, site_url, dev, expected) -> None:
    settings.SITE_URL, settings.DEV = site_url, dev
    assert origin_meta(client.get("/a-propos/").content.decode()) == expected
