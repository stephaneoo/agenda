import json
from pathlib import Path

import pytest
from django.core.management import call_command

from geo import services
from tools.travel_matrix import CommuneInfo, Durations, select_area, to_minutes, write_payload


def square(lat: float, lon: float, h: float = 0.01) -> dict:
    ring = [[lon - h, lat - h], [lon + h, lat - h], [lon + h, lat + h], [lon - h, lat + h], [lon - h, lat - h]]
    return {"type": "MultiPolygon", "coordinates": [[ring]]}


def test_select_area_keeps_origin_and_communes_within_cap() -> None:
    d = Durations(
        insee=["26220", "26182", "26377", "84138", "26198"],
        seconds=[
            # The origin's own entry is unknown here, and the origin is still kept.
            [None, 1800.0, 1800.4, None, 600.0],
            [1800.0, 0.0, 1.0, 1.0, 1.0],
            [1.0, 1.0, 0.0, 1.0, 1.0],
            [1.0, 1.0, 1.0, 0.0, 1.0],
            [1.0, 1.0, 1.0, 1.0, 0.0],
        ],
    )
    # 1800 s is 30 min: kept. 1800.4 s rounds up to 31: dropped. Unreachable: dropped.
    assert select_area(d, "26220", 30) == ["26220", "26182", "26198"]
    assert select_area(d, "26220", 0) == ["26220"]


def test_durations_rounded_up_to_whole_minutes() -> None:
    assert to_minutes(0.0) == 0
    assert to_minutes(0.1) == 1
    assert to_minutes(60.0) == 1
    assert to_minutes(60.1) == 2
    assert to_minutes(1799.9) == 30
    assert to_minutes(None) is None


@pytest.mark.django_db
def test_payload_validates_against_task3_loader(tmp_path: Path) -> None:
    communes = {
        "26220": CommuneInfo("26220", "Nyons", 44.3606, 5.1406, ["26110"], square(44.3606, 5.1406)),
        "26182": CommuneInfo("26182", "Mirabel-aux-Baronnies", 44.311, 5.099, ["26110"], square(44.311, 5.099)),
        "26198": CommuneInfo("26198", "Montélimar", 44.558, 4.751, ["26200"], square(44.558, 4.751)),
    }
    area = Durations(insee=["26220", "26182"], seconds=[[0.0, 541.2], [600.0, 0.0]])
    write_payload(
        tmp_path, version="test", origin="26220", minutes=30,
        area=area, communes=communes, neighbours=["26198"],
    )

    matrix = json.loads((tmp_path / "travel-times.json").read_text())
    assert matrix["minutes"] == [[0, 10], [10, 0]]
    assert matrix["origin_for_area"] == "26220" and matrix["area_minutes"] == 30

    call_command(
        "load_geo", matrix=str(tmp_path / "travel-times.json"), communes=str(tmp_path / "communes.geojson")
    )
    assert {c.insee for c in services.covered_communes()} == {"26220", "26182"}
    assert services.travel_minutes("26220", "26182") == 10
    assert services.resolve_commune(44.558, 4.751, None, None) == "26198"
