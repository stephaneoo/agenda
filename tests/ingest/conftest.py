"""Fixtures for the DATAtourisme sync: the real area, a fake HTTP session, a settable clock."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command

FIXTURES = Path(__file__).parent / "fixtures"
DATA = Path(__file__).resolve().parents[2] / "data"
# 06:00 in Paris on the day after the DST change, when the timer runs.
NOW = datetime(2026, 10, 26, 5, 0, tzinfo=UTC)


def recorded_pages() -> list[dict]:
    """The three recorded pages of the tabular API (Code_postal_et_commune contains 26110)."""
    return [json.loads((FIXTURES / f"page-{n}.json").read_text(encoding="utf-8")) for n in (1, 2, 3)]


def recorded_rows() -> list[dict]:
    return [row for page in recorded_pages() for row in page["data"]]


def recorded_error() -> dict:
    return json.loads((FIXTURES / "error.json").read_text(encoding="utf-8"))


TEMPLATE = recorded_rows()[1]  # "Marché nyonsais du jeudi"


def row(n: int, periods: str = "2026-11-05<->2026-11-05", *, name: str | None = None,
        lat: float | None = 44.360433, lon: float | None = 5.139799, cp: str = "26110#Nyons", **extra: Any) -> dict:
    """A tabular-API row built on a recorded one, with its own id."""
    r = copy.deepcopy(TEMPLATE)
    r.update({
        "__id": n,
        "Nom_du_POI": name or f"Événement {n}",
        "Periodes_regroupees": periods,
        "Latitude": lat, "Longitude": lon,
        "Code_postal_et_commune": cp,
        "URI_ID_du_POI": f"https://data.datatourisme.fr/13/00000000-0000-4000-8000-{n:012d}",
        **extra,
    })
    return r


def pages_of(rows: list[dict], size: int = 25) -> list[tuple[int, dict]]:
    """Rows as the tabular API pages them: (status, body) per page."""
    chunks = [rows[i:i + size] for i in range(0, len(rows), size)] or [[]]
    return [(200, {"data": chunk, "meta": {"page": n, "page_size": size, "total": len(rows)},
                   "links": {"next": None, "prev": None}}) for n, chunk in enumerate(chunks, 1)]


class FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode()
        self.headers = {"Content-Type": "application/json"}

    def json(self) -> Any:
        return self._body


class FakeSession:
    """Answers every query with the same pages: pages[n - 1] for page=n. A page may be an
    exception instance, which is raised, as a network error would be."""

    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def get(self, url: str, params: dict | None = None, timeout: float | None = None) -> FakeResponse:
        params = dict(params or {})
        self.calls.append(params)
        n = int(params.get("page", 1))
        page = self.pages[n - 1] if n <= len(self.pages) else (200, {"data": [], "meta": {"page": n, "total": 0}})
        if isinstance(page, Exception):
            raise page
        return FakeResponse(*page)


@pytest.fixture
def real_geo(db: None) -> None:
    """The committed area: 64 communes within 30 min of Nyons, and their neighbours."""
    call_command("load_geo", matrix=str(DATA / "travel-times.json"), communes=str(DATA / "communes.geojson"))


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    from django.utils import timezone

    c = Clock(NOW)
    monkeypatch.setattr(timezone, "now", c)
    return c


@pytest.fixture(autouse=True)
def status_file(settings: Any, tmp_path: Path) -> Path:
    settings.STATUS_FILE = str(tmp_path / "status.json")
    return tmp_path / "status.json"
