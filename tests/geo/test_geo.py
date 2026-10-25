from pathlib import Path

import pytest
from django.core.management import call_command

from geo import services

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def geo_loaded(db: None) -> None:
    call_command(
        "load_geo",
        matrix=str(FIXTURES / "travel-times.small.json"),
        communes=str(FIXTURES / "communes.small.geojson"),
    )


def test_load_geo_sets_covered_area_by_30_min_from_nyons(geo_loaded: None) -> None:
    covered = {c.insee for c in services.covered_communes()}
    # Valréas (Enclave des Papes) is at exactly 30 min: the cap is inclusive.
    assert covered == {"26220", "26182", "26377", "84138"}


def test_reachable_within_0_is_origin_only(geo_loaded: None) -> None:
    assert services.reachable("26220", 0) == {"26220"}
    assert services.reachable("26220", 12) == {"26220", "26182", "26377"}


def test_travel_minutes_null_when_unreachable(geo_loaded: None) -> None:
    assert services.travel_minutes("26182", "26377") is None
    assert services.travel_minutes("26377", "26182") == 9
    assert "26377" not in services.reachable("26182", 60)


def test_resolve_commune_point_in_polygon(geo_loaded: None) -> None:
    # A point in Valréas resolves to it, whatever the postcode and name say.
    assert services.resolve_commune(44.385, 4.991, "26110", "Nyons") == "84138"
    # A point in a loaded commune outside the covered area resolves to that commune.
    assert services.resolve_commune(44.559, 4.752, None, None) == "26198"


def test_resolve_commune_falls_back_to_postcode_and_name(geo_loaded: None) -> None:
    assert services.resolve_commune(None, None, "26110", "Mirabel aux Baronnies") == "26182"
    assert services.resolve_commune(None, None, "84600", "VALREAS") == "84138"
    # Coordinates that land in no known commune (here lat/lon swapped) fall back too.
    assert services.resolve_commune(5.1406, 44.3606, "26110", "Nyons") == "26220"


def test_resolve_commune_returns_none_when_ambiguous(geo_loaded: None) -> None:
    # 26110 is shared by several communes: a postcode alone never decides.
    assert services.resolve_commune(None, None, "26110", None) is None
    assert services.resolve_commune(None, None, "26110", "Valréas") is None
    assert services.resolve_commune(None, None, None, "Nyons") is None


def test_matrix_payload_shape(geo_loaded: None) -> None:
    payload = services.matrix_payload()
    assert set(payload) == {"version", "communes", "minutes"}
    assert payload["version"] == "test-small-1"
    assert payload["communes"] == ["26182", "26220", "26377", "84138"]
    m = payload["minutes"]
    assert len(m) == 4 and all(len(row) == 4 for row in m)
    assert all(v is None or isinstance(v, int) for row in m for v in row)
    i, j = payload["communes"].index("26220"), payload["communes"].index("84138")
    assert m[i][j] == 30
    assert m[payload["communes"].index("26182")][payload["communes"].index("26377")] is None
