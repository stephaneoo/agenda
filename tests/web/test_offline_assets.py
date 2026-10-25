import json
import re
from datetime import date
from pathlib import Path

import pytest
from django.contrib.staticfiles import finders

from web import views

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "filter-cases.json").read_text(encoding="utf-8"))
REFERENCE = ["/api/v1/communes", "/api/v1/travel-times", "/api/v1/categories", "/api/v1/status"]


def precache(body: str) -> list[str]:
    m = re.search(r"const PRECACHE = (\[.*?\]);", body, re.S)
    assert m, "no PRECACHE list in sw.js"
    return json.loads(m[1])


def test_service_worker_served_at_root_scope(client, db) -> None:
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response["Service-Worker-Allowed"] == "/"
    assert response["Content-Type"].startswith("text/javascript")
    assert "no-cache" in response["Cache-Control"]
    body = response.content.decode()
    urls = precache(body)
    assert set(REFERENCE) <= set(urls)
    assert "/" in urls
    for url in urls:
        if url.startswith("/static/"):
            assert finders.find(url.removeprefix("/static/")), url
    # The cache is named after the content it holds, so a deploy that changes it replaces it.
    assert re.search(r'const VERSION = "[0-9a-f]{16}";', body)


def test_pages_load_the_app_as_a_module_and_carry_its_hooks(client, small_geo) -> None:
    html = client.get("/").content.decode()
    assert '<script type="module" src="/static/web/app.js"></script>' in html
    # No inline script (the CSP forbids them); JSON-LD is data, and only on occurrence pages.
    assert all("src=" in tag for tag in re.findall(r"<script\b[^>]*>", html))
    assert 'id="filters"' in html and 'id="results"' in html
    assert re.search(r'<p id="offline-banner"[^>]*\bhidden\b', html)
    assert 'data-page="agenda"' in html


@pytest.mark.parametrize("case", FIXTURE["periods"], ids=lambda c: f"{c['period']}-{c['today']}")
def test_period_choices_shared_with_the_offline_filter(case: dict) -> None:
    got = views.period_range(case["period"], date.fromisoformat(case["today"]))
    assert got == (date.fromisoformat(case["from"]), date.fromisoformat(case["to"]))
