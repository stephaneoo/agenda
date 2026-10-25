import json
from pathlib import Path

import pytest
from django.apps import apps
from django.core.management import CommandError, call_command
from django.db import connection, models

from geo import services
from geo.models import Commune

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
    # Valréas -> Vinsobres is 0 min in the fixture, and within=0 still means the origin only.
    assert services.reachable("84138", 0) == {"84138"}
    assert "26377" in services.reachable("84138", 1)
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
    # Missing or invalid coordinates fall back.
    assert services.resolve_commune(None, 5.1406, "26110", "Nyons") == "26220"
    assert services.resolve_commune(95.0, 5.1406, "26110", "Nyons") == "26220"
    assert services.resolve_commune(float("nan"), 5.1406, "26110", "Nyons") == "26220"


def test_resolve_commune_valid_point_outside_every_commune_is_none(geo_loaded: None) -> None:
    # A real point that lands nowhere we know: outside the area, or wrong data. Never guessed.
    assert services.resolve_commune(43.0, 5.0, "26110", "Nyons") is None
    assert services.resolve_commune(5.1406, 44.3606, "26110", "Nyons") is None  # lat/lon swapped


def test_resolve_commune_on_shared_border_uses_address_or_gives_up(geo_loaded: None) -> None:
    on_border = (44.60, 5.70)
    assert services.resolve_commune(*on_border, "26110", "La Scie") == "26994"
    assert services.resolve_commune(*on_border, "26110", "le moulin") == "26993"
    assert services.resolve_commune(*on_border, None, None) is None
    assert services.resolve_commune(*on_border, "26110", "Nyons") is None


def test_normalize_name_moves_trailing_article() -> None:
    assert services.normalize_name("Pilles (Les)") == services.normalize_name("Les Pilles")
    assert services.normalize_name("Île (L')") == services.normalize_name("L'Île")
    assert services.normalize_name("ST MAURICE SUR EYGUES") == "saint maurice sur eygues"


def test_resolve_commune_returns_none_when_ambiguous(geo_loaded: None) -> None:
    # Two communes share postcode 26110 and, once normalised, the name "Les Granges".
    assert services.resolve_commune(None, None, "26110", "Les Granges") is None
    assert services.resolve_commune(None, None, "26110", "Granges (Les)") is None
    # A postcode alone never decides, nor a name alone, nor a name under the wrong postcode.
    assert services.resolve_commune(None, None, "26110", None) is None
    assert services.resolve_commune(None, None, None, "Nyons") is None
    assert services.resolve_commune(None, None, "26110", "Valréas") is None


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


@pytest.mark.django_db
def test_matrix_payload_empty_db() -> None:
    assert services.matrix_payload() == {"version": None, "communes": [], "minutes": []}


def test_every_reference_to_commune_is_protected() -> None:
    refs = [
        f for model in apps.get_models() for f in model._meta.get_fields()
        if f.is_relation and f.related_model is Commune and f.concrete
    ]
    assert refs
    assert all(f.remote_field.on_delete is models.PROTECT for f in refs)


def test_load_geo_refuses_to_remove_a_referenced_commune(geo_loaded: None, tmp_path: Path) -> None:
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE scratch_ref (insee varchar(5) REFERENCES geo_commune (insee))")
        cursor.execute("INSERT INTO scratch_ref VALUES ('26198')")
    communes = json.loads((FIXTURES / "communes.small.geojson").read_text())
    communes["features"] = [f for f in communes["features"] if f["properties"]["insee"] != "26198"]
    matrix = json.loads((FIXTURES / "travel-times.small.json").read_text())
    i = [c["insee"] for c in matrix["communes"]].index("26198")
    matrix["communes"].pop(i)
    matrix["minutes"] = [row[:i] + row[i + 1:] for k, row in enumerate(matrix["minutes"]) if k != i]
    (tmp_path / "c.geojson").write_text(json.dumps(communes))
    (tmp_path / "m.json").write_text(json.dumps(matrix))

    with pytest.raises(CommandError, match="26198"):
        call_command("load_geo", matrix=str(tmp_path / "m.json"), communes=str(tmp_path / "c.geojson"))
    assert Commune.objects.filter(insee="26198").exists()
    assert {c.insee for c in services.covered_communes()} == {"26220", "26182", "26377", "84138"}


# --- Malformed input: a CommandError, and nothing loaded. ---

from collections.abc import Callable  # noqa: E402
from typing import Any  # noqa: E402

from geo.management.commands import load_geo as load_geo_module  # noqa: E402

BOWTIE = {
    "type": "MultiPolygon",
    "coordinates": [[[[5.12, 44.34], [5.16, 44.38], [5.16, 44.34], [5.12, 44.38], [5.12, 44.34]]]],
}


def first_props(c: dict) -> dict:
    return c["features"][0]["properties"]


COMMUNE_BREAKAGES: dict[str, Callable[[dict], Any]] = {
    "not a collection": lambda c: c.clear(),
    "features not a list": lambda c: c.update(features={"a": 1}),
    "no properties": lambda c: c["features"][0].pop("properties"),
    "insee too short": lambda c: first_props(c).update(insee="2622"),
    "insee letters": lambda c: first_props(c).update(insee="26C20"),
    "insee not a string": lambda c: first_props(c).update(insee=26220),
    "postcode too short": lambda c: first_props(c).update(postal_codes=["2611"]),
    "postcode with letters": lambda c: first_props(c).update(postal_codes=["2611O"]),
    "postcodes not a list": lambda c: first_props(c).update(postal_codes="26110"),
    "name missing": lambda c: first_props(c).pop("name"),
    "name too long": lambda c: first_props(c).update(name="x" * 101),
    "lat not a number": lambda c: first_props(c).update(lat="north"),
    "lat out of range": lambda c: first_props(c).update(lat=95.0),
    "lon NaN": lambda c: first_props(c).update(lon=float("nan")),
    "self-intersecting polygon": lambda c: c["features"][0].update(geometry=BOWTIE),
    "geometry garbage": lambda c: c["features"][0].update(geometry={"type": "Blob", "coordinates": 1}),
    "geometry a point": lambda c: c["features"][0].update(geometry={"type": "Point", "coordinates": [5.1, 44.3]}),
    "duplicate insee": lambda c: c["features"].append(json.loads(json.dumps(c["features"][0]))),
}

MATRIX_BREAKAGES: dict[str, Callable[[dict], Any]] = {
    "not an object": lambda m: m.clear(),
    "commune without insee": lambda m: m["communes"][0].pop("insee"),
    "commune insee malformed": lambda m: m["communes"][0].update(insee="2A2B0"),
    "origin malformed": lambda m: m.update(origin_for_area="26220; DROP"),
    "area_minutes a string": lambda m: m.update(area_minutes="30"),
    "minutes row not a list": lambda m: m["minutes"].__setitem__(0, 5),
    "version not a string": lambda m: m.update(version=["x"]),
}


def load_broken(tmp_path: Path, communes: dict | None = None, matrix: dict | None = None) -> None:
    c = communes if communes is not None else json.loads((FIXTURES / "communes.small.geojson").read_text())
    m = matrix if matrix is not None else json.loads((FIXTURES / "travel-times.small.json").read_text())
    (tmp_path / "c.geojson").write_text(json.dumps(c))
    (tmp_path / "m.json").write_text(json.dumps(m))
    call_command("load_geo", matrix=str(tmp_path / "m.json"), communes=str(tmp_path / "c.geojson"))


@pytest.mark.django_db
@pytest.mark.parametrize("breakage", COMMUNE_BREAKAGES, ids=list(COMMUNE_BREAKAGES))
def test_load_geo_rejects_malformed_communes(tmp_path: Path, breakage: str) -> None:
    c = json.loads((FIXTURES / "communes.small.geojson").read_text())
    if breakage == "not a collection":
        c = []  # type: ignore[assignment]
    else:
        COMMUNE_BREAKAGES[breakage](c)
    with pytest.raises(CommandError):
        load_broken(tmp_path, communes=c)
    assert not Commune.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("breakage", MATRIX_BREAKAGES, ids=list(MATRIX_BREAKAGES))
def test_load_geo_rejects_malformed_matrix(tmp_path: Path, breakage: str) -> None:
    m = json.loads((FIXTURES / "travel-times.small.json").read_text())
    if breakage == "not an object":
        m = []  # type: ignore[assignment]
    else:
        MATRIX_BREAKAGES[breakage](m)
    with pytest.raises(CommandError):
        load_broken(tmp_path, matrix=m)
    assert not Commune.objects.exists()


@pytest.mark.django_db
def test_load_geo_accepts_corsican_insee(tmp_path: Path) -> None:
    c = json.loads((FIXTURES / "communes.small.geojson").read_text())
    extra = json.loads(json.dumps(c["features"][-1]))
    extra["properties"].update(insee="2A004", name="Ajaccio", postal_codes=["20000"])
    c["features"].append(extra)
    load_broken(tmp_path, communes=c)
    assert Commune.objects.filter(insee="2A004").exists()


@pytest.mark.django_db
def test_load_geo_caps_input_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(load_geo_module, "MAX_INPUT_BYTES", 1000)
    with pytest.raises(CommandError, match="too large"):
        load_broken(tmp_path)
    assert not Commune.objects.exists()
