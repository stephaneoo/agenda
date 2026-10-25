"""Generate data/travel-times.json and data/communes.geojson with OSRM.

    nix run .#travel-matrix -- --origin 26220 --minutes 30

Downloads the Geofabrik extracts and IGN ADMIN EXPRESS COG, routes with OSRM's car
profile between communes' chef-lieu points, keeps the communes within --minutes of
--origin, and writes the two files that `manage.py load_geo` reads. Downloads and OSRM
files are cached in --workdir. The Nix app provides osmium, osrm-*, ogr2ogr and 7z.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

GEOFABRIK = "https://download.geofabrik.de/europe/france/{}-latest.osm.pbf"
# Drôme and Vaucluse are in Rhône-Alpes and PACA. Languedoc-Roussillon is added for the
# Gard, a neighbour of Vaucluse: without its roads, Gard points would snap across the Rhône.
REGIONS = ["rhone-alpes", "provence-alpes-cote-d-azur", "languedoc-roussillon"]
ADMIN_EXPRESS_EDITION = "ADMIN-EXPRESS-COG_4-0__GPKG_LAMB93_FXX_2026-01-01"
ADMIN_EXPRESS = (
    "https://data.geopf.fr/telechargement/download/ADMIN-EXPRESS-COG/"
    f"{ADMIN_EXPRESS_EDITION}/{ADMIN_EXPRESS_EDITION}.7z"
)
# La Poste's official postcode base: INSEE code -> postcodes. Admin Express has none.
LAPOSTE_POSTCODES = "https://data.laposte.fr/data-fair/api/v1/datasets/laposte-hexasmal/raw"
# Drôme, Vaucluse and their neighbouring departments.
DEPARTMENTS = ["26", "84", "04", "05", "07", "13", "30", "38"]
OSRM_PORT = 5917
# Geofabrik's *-latest files change daily; a week-old copy is refreshed.
EXTRACT_MAX_AGE_DAYS = 7


@dataclass(frozen=True)
class CommuneInfo:
    insee: str
    name: str
    lat: float  # chef-lieu
    lon: float
    postal_codes: list[str]
    geometry: dict  # GeoJSON MultiPolygon, WGS84


@dataclass(frozen=True)
class Durations:
    """OSRM durations in seconds: seconds[i][j] from insee[i] to insee[j]. None: no route."""

    insee: list[str]
    seconds: list[list[float | None]]

    def subset(self, keep: list[str]) -> Durations:
        idx = [self.insee.index(i) for i in keep]
        return Durations(list(keep), [[self.seconds[i][j] for j in idx] for i in idx])


def to_minutes(seconds: float | None) -> int | None:
    """Whole minutes, rounded up, so that a 30-minute cap never admits 30 min 1 s."""
    if seconds is None:
        return None
    # OSRM reports tenths of a second; round first so float noise can't push 60.0 to 2 min.
    return math.ceil(round(seconds, 3) / 60)


def select_area(durations: Durations, origin: str, minutes: int) -> list[str]:
    """The origin plus every commune reachable from it within `minutes`, in input order."""
    row = durations.seconds[durations.insee.index(origin)]
    return [
        insee
        for insee, s in zip(durations.insee, row)
        if insee == origin or ((m := to_minutes(s)) is not None and m <= minutes)
    ]


def write_payload(
    out_dir: Path,
    *,
    version: str,
    origin: str,
    minutes: int,
    area: Durations,
    communes: Mapping[str, CommuneInfo],
    neighbours: Iterable[str] = (),
) -> None:
    """Write travel-times.json (the area's matrix) and communes.geojson (area + neighbours).

    The neighbours' polygons are kept so that a point just outside the area resolves to
    the commune it is really in, not to nothing.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    matrix = {
        "version": version,
        "origin_for_area": origin,
        "area_minutes": minutes,
        "communes": [
            {"insee": i, "name": communes[i].name, "lat": communes[i].lat, "lon": communes[i].lon}
            for i in area.insee
        ],
        "minutes": [[to_minutes(s) for s in row] for row in area.seconds],
    }
    (out_dir / "travel-times.json").write_text(json.dumps(matrix, ensure_ascii=False) + "\n", encoding="utf-8")

    features = [
        {
            "type": "Feature",
            "properties": {
                "insee": c.insee,
                "name": c.name,
                "postal_codes": sorted(c.postal_codes),
                "lat": c.lat,
                "lon": c.lon,
                "covered": c.insee in area.insee,
            },
            "geometry": c.geometry,
        }
        for c in sorted((communes[i] for i in {*area.insee, *neighbours}), key=lambda c: c.insee)
    ]
    geojson = {"type": "FeatureCollection", "features": features}
    (out_dir / "communes.geojson").write_text(
        json.dumps(geojson, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )


# --- The pipeline. Everything below needs the tools from nix/travel-matrix.nix. ---


def log(msg: str) -> None:
    print(f"[travel-matrix {datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def run(*cmd: str | Path) -> None:
    log(" ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True)


def is_stale(path: Path, max_age_days: int | None, now: float | None = None) -> bool:
    if not path.exists():
        return True
    if max_age_days is None:
        return False
    return (now if now is not None else time.time()) - path.stat().st_mtime > max_age_days * 86400


def download(url: str, dest: Path, max_age_days: int | None = None) -> Path:
    if not is_stale(dest, max_age_days):
        return dest
    log(f"downloading {url}")
    part = dest.with_name(dest.name + ".part")
    run("curl", "-fL", "--retry", "3", "-o", part, url)
    part.rename(dest)
    return dest


def ogr_cache_name(layer: str, where: str) -> str:
    """A cache file per layer and filter, so changing DEPARTMENTS can't reuse a stale extract."""
    digest = hashlib.sha256(f"{ADMIN_EXPRESS_EDITION}\0{layer}\0{where}".encode()).hexdigest()[:12]
    return f"{layer}-{digest}.geojson"


def ogr_to_geojson(gpkg: Path, layer: str, where: str, workdir: Path) -> dict:
    out = workdir / ogr_cache_name(layer, where)
    if not out.exists():
        tmp = out.with_name(out.name + ".part")
        tmp.unlink(missing_ok=True)
        run(
            "ogr2ogr", "-f", "GeoJSON", "-t_srs", "EPSG:4326", "-lco", "COORDINATE_PRECISION=6",
            "-where", where, tmp, gpkg, layer,
        )
        tmp.rename(out)
    return json.loads(out.read_text(encoding="utf-8"))


def load_admin_express(workdir: Path) -> tuple[dict[str, dict], dict[str, tuple[float, float]]]:
    """Commune features and chef-lieu points (lat, lon) by INSEE code."""
    archive = download(ADMIN_EXPRESS, workdir / "downloads" / f"{ADMIN_EXPRESS_EDITION}.7z")
    extract_dir = workdir / "admin-express"
    if not extract_dir.exists():
        run("7z", "x", "-y", f"-o{extract_dir}.part", archive)
        Path(f"{extract_dir}.part").rename(extract_dir)
    gpkg = next(extract_dir.rglob("*.gpkg"))
    deps = ",".join(f"'{d}'" for d in DEPARTMENTS)
    where = f"code_insee_du_departement IN ({deps})"
    communes = ogr_to_geojson(gpkg, "commune", where, workdir)
    chefs = ogr_to_geojson(gpkg, "chef_lieu_de_commune", "code_insee_de_la_commune IS NOT NULL", workdir)
    by_insee = {f["properties"]["code_insee"]: f for f in communes["features"]}
    points = {}
    for f in chefs["features"]:
        insee = f["properties"]["code_insee_de_la_commune"]
        if insee in by_insee and f["geometry"]:
            lon, lat = f["geometry"]["coordinates"][:2]
            points[insee] = (lat, lon)
    return by_insee, points


def load_postcodes(workdir: Path) -> dict[str, set[str]]:
    path = download(LAPOSTE_POSTCODES, workdir / "downloads" / "laposte-hexasmal.csv")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:  # the file has been published in Latin-1
        text = raw.decode("latin-1")
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,")
    codes: dict[str, set[str]] = {}
    for row in csv.DictReader(text.splitlines(), dialect=dialect):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        insee = row.get("code_commune_insee") or row.get("#code_commune_insee")
        cp = row.get("code_postal")
        if insee and cp:
            codes.setdefault(insee, set()).add(cp)
    return codes


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def prepare_osrm(workdir: Path, bbox: tuple[float, float, float, float]) -> Path:
    """Merge the extracts, clip them to bbox (to fit in memory), and build the MLD graph."""
    pbfs = [
        download(GEOFABRIK.format(r), workdir / "downloads" / f"{r}-latest.osm.pbf", EXTRACT_MAX_AGE_DAYS)
        for r in REGIONS
    ]
    # The graph is keyed on the bbox and the extracts it was built from.
    key = repr((bbox, [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in pbfs]))
    osrm_dir = workdir / f"osrm-{hashlib.sha256(key.encode()).hexdigest()[:12]}"
    graph = osrm_dir / "area.osrm"
    if (osrm_dir / "READY").exists():
        return graph
    shutil.rmtree(osrm_dir, ignore_errors=True)
    osrm_dir.mkdir(parents=True)
    merged = osrm_dir / "merged.osm.pbf"
    run("osmium", "merge", "--overwrite", "-o", merged, *pbfs)
    clipped = osrm_dir / "area.osm.pbf"
    run("osmium", "extract", "--overwrite", "-b", ",".join(f"{v:.4f}" for v in bbox), "-o", clipped, merged)
    merged.unlink()
    run("osrm-extract", "-p", os.environ["OSRM_CAR_PROFILE"], clipped)
    run("osrm-partition", graph)
    run("osrm-customize", graph)
    (osrm_dir / "READY").touch()
    return graph


def osm_timestamp(workdir: Path) -> str:
    """The oldest replication timestamp among the extracts, as a date."""
    stamps = []
    for r in REGIONS:
        out = subprocess.run(
            ["osmium", "fileinfo", "-g", "header.option.osmosis_replication_timestamp",
             workdir / "downloads" / f"{r}-latest.osm.pbf"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if out:
            stamps.append(out[:10])
    return min(stamps) if stamps else "unknown"


def osrm_table(graph: Path, points: list[tuple[float, float]]) -> list[list[float | None]]:
    proc = subprocess.Popen(
        ["osrm-routed", "--algorithm", "mld", "-i", "127.0.0.1", "-p", str(OSRM_PORT),
         "--max-table-size", str(max(100, len(points))), str(graph)],
    )
    try:
        coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
        url = f"http://127.0.0.1:{OSRM_PORT}/table/v1/driving/{coords}?annotations=duration"
        for attempt in range(120):
            try:
                with urllib.request.urlopen(url, timeout=600) as resp:
                    body = json.load(resp)
                break
            except (ConnectionError, urllib.error.URLError):
                if proc.poll() is not None:
                    raise RuntimeError("osrm-routed exited") from None
                time.sleep(1)
        else:
            raise RuntimeError("osrm-routed did not answer")
    finally:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=60)
    if body.get("code") != "Ok":
        raise RuntimeError(f"OSRM table: {body.get('code')} {body.get('message')}")
    for i, wp in enumerate(body["sources"]):
        if wp.get("distance", 0) > 2000:
            log(f"warning: point {i} snapped {wp['distance']:.0f} m to the road network")
    return body["durations"]


def neighbours_of(area: list[str], features: Mapping[str, dict]) -> list[str]:
    """Communes outside the area whose territory touches it."""
    from django.contrib.gis.geos import GEOSGeometry

    geoms = {i: GEOSGeometry(json.dumps(f["geometry"]), srid=4326) for i, f in features.items()}
    union = geoms[area[0]]
    for i in area[1:]:
        union = union.union(geoms[i])
    # A small buffer absorbs slivers between polygons that should share a border.
    zone = union.buffer(0.0005).prepared
    return sorted(i for i, g in geoms.items() if i not in area and zone.intersects(g))


def admin_postcodes(feature: dict) -> set[str]:
    return set(re.findall(r"\b\d{5}\b", feature["properties"].get("code_postal") or ""))


def multipolygon(geometry: dict) -> dict:
    if geometry["type"] == "Polygon":
        return {"type": "MultiPolygon", "coordinates": [geometry["coordinates"]]}
    return geometry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--origin", default="26220", help="INSEE code of the area's centre")
    parser.add_argument("--minutes", type=int, default=30, help="travel-time cap for the covered area")
    parser.add_argument("--out", type=Path, default=Path("data"))
    parser.add_argument(
        "--workdir", type=Path,
        default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "agenda-travel-matrix",
    )
    args = parser.parse_args(argv)

    from django.conf import settings

    if not settings.configured:
        settings.configure(
            GEOS_LIBRARY_PATH=os.environ.get("GEOS_LIBRARY_PATH"),
            GDAL_LIBRARY_PATH=os.environ.get("GDAL_LIBRARY_PATH"),
        )

    args.workdir.mkdir(parents=True, exist_ok=True)
    features, chefs = load_admin_express(args.workdir)
    postcodes = load_postcodes(args.workdir)
    if args.origin not in chefs:
        parser.error(f"unknown origin {args.origin}")
    origin_pt = chefs[args.origin]

    # No car covers more than 2 km of straight line per minute on these roads, so this
    # radius can't miss a commune of the area. Routes may leave it; the bbox has a margin.
    radius = args.minutes * 2
    candidates = sorted(i for i, p in chefs.items() if distance_km(origin_pt, p) <= radius)
    lats = [chefs[i][0] for i in candidates]
    lons = [chefs[i][1] for i in candidates]
    bbox = (min(lons) - 0.4, min(lats) - 0.3, max(lons) + 0.4, max(lats) + 0.3)
    log(f"{len(candidates)} candidate communes within {radius} km of {args.origin}")

    graph = prepare_osrm(args.workdir, bbox)
    seconds = osrm_table(graph, [chefs[i] for i in candidates])
    full = Durations(candidates, seconds)
    area = select_area(full, args.origin, args.minutes)
    neighbours = neighbours_of(area, features)

    communes = {
        i: CommuneInfo(
            insee=i,
            name=features[i]["properties"]["nom_officiel"],
            lat=chefs[i][0] if i in chefs else 0.0,
            lon=chefs[i][1] if i in chefs else 0.0,
            postal_codes=sorted(postcodes.get(i, set()) | admin_postcodes(features[i])),
            geometry=multipolygon(features[i]["geometry"]),
        )
        for i in {*area, *neighbours}
        if i in chefs
    }
    missing = sorted({*area, *neighbours} - set(communes))
    if missing:
        log(f"warning: no chef-lieu for {', '.join(missing)}; left out")
        neighbours = [i for i in neighbours if i in communes]
    no_postcode = sorted(i for i, c in communes.items() if not c.postal_codes)
    if no_postcode:
        log(f"warning: no postcode for {', '.join(no_postcode)}")

    version = f"{datetime.now(UTC):%Y-%m-%d} osm:{osm_timestamp(args.workdir)} ign:{ADMIN_EXPRESS_EDITION[-10:]}"
    write_payload(
        args.out, version=version, origin=args.origin, minutes=args.minutes,
        area=full.subset(area), communes=communes, neighbours=neighbours,
    )
    log(f"area: {len(area)} communes within {args.minutes} min of {args.origin}; {len(neighbours)} neighbours")
    for i in sorted(area, key=lambda i: communes[i].name):
        m = to_minutes(full.seconds[candidates.index(args.origin)][candidates.index(i)])
        print(f"{i}\t{m}\t{communes[i].name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
