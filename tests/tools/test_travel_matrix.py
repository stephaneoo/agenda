import json
import os
from pathlib import Path

import pytest
from django.core.management import call_command

from geo import services
from tools.travel_matrix import (
    CommuneInfo,
    Durations,
    is_stale,
    ogr_cache_name,
    select_area,
    to_minutes,
    write_payload,
)


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


def test_ogr_cache_name_depends_on_layer_and_filter() -> None:
    a = ogr_cache_name("commune", "code_insee_du_departement IN ('26','84')")
    assert a == ogr_cache_name("commune", "code_insee_du_departement IN ('26','84')")
    assert a != ogr_cache_name("commune", "code_insee_du_departement IN ('26')")
    assert a != ogr_cache_name("chef_lieu_de_commune", "code_insee_du_departement IN ('26','84')")
    assert a.endswith(".geojson")


def test_extracts_older_than_a_week_are_stale(tmp_path: Path) -> None:
    f = tmp_path / "x.osm.pbf"
    assert is_stale(f, max_age_days=7, now=1_000_000_000)  # missing
    f.touch()
    os.utime(f, (1_000_000_000, 1_000_000_000))
    assert not is_stale(f, max_age_days=7, now=1_000_000_000 + 6 * 86400)
    assert is_stale(f, max_age_days=7, now=1_000_000_000 + 8 * 86400)
    assert not is_stale(f, max_age_days=None, now=1_000_000_000 + 800 * 86400)


# --- Downloads: URLs, checksums, privileges. ---

import hashlib  # noqa: E402

import tools.travel_matrix as tm  # noqa: E402


def test_configured_urls_pass_validation() -> None:
    for url in [tm.ADMIN_EXPRESS, tm.LAPOSTE_POSTCODES, *(tm.GEOFABRIK.format(r) for r in tm.REGIONS)]:
        tm.check_url(url)
    tm.check_url(tm.GEOFABRIK.format("rhone-alpes") + ".md5")


@pytest.mark.parametrize(
    "url",
    [
        "http://download.geofabrik.de/europe/france/rhone-alpes-latest.osm.pbf",
        "https://evil.example/europe/france/rhone-alpes-latest.osm.pbf",
        "https://download.geofabrik.de.evil.example/x.osm.pbf",
        "https://user@download.geofabrik.de/x.osm.pbf",
        "https://download.geofabrik.de:8443/x.osm.pbf",
        "file:///etc/passwd",
        "-o/etc/passwd",
    ],
)
def test_bad_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        tm.check_url(url)


def test_verify_sha256(tmp_path: Path) -> None:
    f = tmp_path / "a.7z"
    f.write_bytes(b"archive")
    tm.verify_sha256(f, hashlib.sha256(b"archive").hexdigest())
    tm.verify_sha256(f, hashlib.sha256(b"archive").hexdigest().upper())
    with pytest.raises(tm.ChecksumError, match="a.7z"):
        tm.verify_sha256(f, hashlib.sha256(b"other").hexdigest())


def test_parse_md5_file() -> None:
    h = "9f6954c35062b005a8e6dc200c2b3148"
    assert tm.parse_md5_file(f"{h}  rhone-alpes-latest.osm.pbf\n", "rhone-alpes-latest.osm.pbf") == h
    assert tm.parse_md5_file(f"{h.upper()} *rhone-alpes-latest.osm.pbf", "rhone-alpes-latest.osm.pbf") == h
    with pytest.raises(tm.ChecksumError):
        tm.parse_md5_file(f"{h}  other.osm.pbf\n", "rhone-alpes-latest.osm.pbf")
    with pytest.raises(tm.ChecksumError):
        tm.parse_md5_file("not-a-hash  rhone-alpes-latest.osm.pbf\n", "rhone-alpes-latest.osm.pbf")
    with pytest.raises(tm.ChecksumError):
        tm.parse_md5_file("<html>404</html>", "rhone-alpes-latest.osm.pbf")


def test_verify_md5(tmp_path: Path) -> None:
    f = tmp_path / "r-latest.osm.pbf"
    f.write_bytes(b"pbf")
    tm.verify_md5(f, f"{hashlib.md5(b'pbf').hexdigest()}  r-latest.osm.pbf\n")
    with pytest.raises(tm.ChecksumError):
        tm.verify_md5(f, f"{hashlib.md5(b'x').hexdigest()}  r-latest.osm.pbf\n")


def test_tools_drop_root_privileges() -> None:
    assert tm.unprivileged_prefix(0) == ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups"]
    assert tm.unprivileged_prefix(1000) == []


def fake_curl(monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(*cmd: object) -> None:
        args = [str(c) for c in cmd]
        calls.append(args)
        Path(args[args.index("-o") + 1]).write_bytes(body)

    monkeypatch.setattr(tm, "run", run)
    return calls


def test_download_https_only_and_pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = fake_curl(monkeypatch, b"archive")
    dest = tmp_path / "a.7z"
    tm.download(tm.ADMIN_EXPRESS, dest, sha256=hashlib.sha256(b"archive").hexdigest())
    assert dest.read_bytes() == b"archive"
    curl = calls[0][calls[0].index("curl"):]
    assert curl[curl.index("--proto") + 1] == "=https"
    assert curl[curl.index("--proto-redir") + 1] == "=https"


def test_download_checksum_mismatch_keeps_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_curl(monkeypatch, b"tampered")
    dest = tmp_path / "a.7z"
    with pytest.raises(tm.ChecksumError):
        tm.download(tm.ADMIN_EXPRESS, dest, sha256=hashlib.sha256(b"archive").hexdigest())
    assert list(tmp_path.iterdir()) == []


def test_download_refuses_bad_url_before_fetching(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = fake_curl(monkeypatch, b"x")
    with pytest.raises(ValueError):
        tm.download("http://data.geopf.fr/x.7z", tmp_path / "x.7z")
    assert calls == []
