"""Load communes and the travel-time matrix, and derive the covered area.

    manage.py load_geo --matrix data/travel-times.json --communes data/communes.geojson

Replaces whatever was loaded before, in one transaction. Malformed input (bad codes,
coordinates or geometry, files over MAX_INPUT_BYTES) is refused with a CommandError
before anything is written.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from django.contrib.gis.gdal import GDALException
from django.contrib.gis.geos import GEOSException, GEOSGeometry, MultiPolygon, Point
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from geo.models import Commune, CoveredArea, MatrixInfo, TravelTime


# The real files are a few MB; this bounds memory and GEOS work on a bad or hostile file.
MAX_INPUT_BYTES = 50 * 1024 * 1024
# Five characters: the department (2A and 2B for Corsica), then the commune.
INSEE = re.compile(r"^\d[\dAB]\d{3}$")
POSTCODE = re.compile(r"^\d{5}$")


def _read_json(path: str) -> Any:
    try:
        size = Path(path).stat().st_size
        if size > MAX_INPUT_BYTES:
            raise CommandError(f"{path}: too large ({size} bytes, at most {MAX_INPUT_BYTES})")
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CommandError(f"{path}: {e}") from e


def _insee(value: Any, where: str) -> str:
    if not isinstance(value, str) or not INSEE.match(value):
        raise CommandError(f"{where}: bad INSEE code {value!r}")
    return value


def _coordinate(value: Any, limit: float, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > limit:
        raise CommandError(f"{where}: bad coordinate {value!r}")
    return float(value)


def _commune(feature: Any) -> Commune:
    if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
        raise CommandError("communes: a feature has no properties")
    p = feature["properties"]
    insee = _insee(p.get("insee"), "communes")
    where = f"commune {insee}"
    name = p.get("name")
    if not isinstance(name, str) or not 0 < len(name) <= 100:
        raise CommandError(f"{where}: bad name {name!r}")
    postal_codes = p.get("postal_codes") or []
    if not isinstance(postal_codes, list) or not all(isinstance(c, str) and POSTCODE.match(c) for c in postal_codes):
        raise CommandError(f"{where}: bad postcodes {postal_codes!r}")
    lat = _coordinate(p.get("lat"), 90, where)
    lon = _coordinate(p.get("lon"), 180, where)
    try:
        geom = GEOSGeometry(json.dumps(feature.get("geometry")), srid=4326)
    except (GEOSException, GDALException, ValueError, TypeError) as e:
        raise CommandError(f"{where}: unreadable geometry ({e})") from e
    if geom.geom_type == "Polygon":
        geom = MultiPolygon(geom, srid=4326)
    if geom.geom_type != "MultiPolygon":
        raise CommandError(f"{where}: unexpected geometry {geom.geom_type}")
    # An invalid polygon gives wrong point-in-polygon answers; fix the source instead.
    if not geom.valid:
        raise CommandError(f"{where}: invalid geometry ({geom.valid_reason})")
    return Commune(
        insee=insee, name=name, chef_lieu=Point(lon, lat, srid=4326), geom=geom, postal_codes=sorted(postal_codes)
    )


def _communes(geojson: Any) -> dict[str, Commune]:
    if not isinstance(geojson, dict) or not isinstance(geojson.get("features"), list):
        raise CommandError("communes: not a GeoJSON FeatureCollection")
    communes: dict[str, Commune] = {}
    for feature in geojson["features"]:
        commune = _commune(feature)
        if commune.insee in communes:
            raise CommandError(f"communes: duplicate commune {commune.insee}")
        communes[commune.insee] = commune
    return communes


def _validate_matrix(matrix: Any, known: set[str]) -> list[str]:
    if not isinstance(matrix, dict):
        raise CommandError("matrix: not a JSON object")
    for key in ("version", "origin_for_area", "area_minutes", "communes", "minutes"):
        if key not in matrix:
            raise CommandError(f"matrix: missing {key}")
    if not isinstance(matrix["version"], str) or not 0 < len(matrix["version"]) <= 100:
        raise CommandError(f"matrix: bad version {matrix['version']!r}")
    cap = matrix["area_minutes"]
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        raise CommandError(f"matrix: bad area_minutes {cap!r}")
    _insee(matrix["origin_for_area"], "matrix origin")
    if not isinstance(matrix["communes"], list) or not all(isinstance(c, dict) for c in matrix["communes"]):
        raise CommandError("matrix: communes must be a list of objects")
    order = [_insee(c.get("insee"), "matrix") for c in matrix["communes"]]
    n = len(order)
    if len(set(order)) != n:
        raise CommandError("matrix: duplicate communes")
    rows = matrix["minutes"]
    if not isinstance(rows, list) or len(rows) != n or any(not isinstance(row, list) or len(row) != n for row in rows):
        raise CommandError(f"matrix: minutes must be {n}x{n}")
    for row in matrix["minutes"]:
        for v in row:
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                raise CommandError(f"matrix: bad duration {v!r}")
    if missing := sorted(set(order) - known):
        raise CommandError(f"matrix: communes missing from the communes file: {', '.join(missing)}")
    if matrix["origin_for_area"] not in order:
        raise CommandError(f"matrix: origin {matrix['origin_for_area']} not in the matrix")
    return order


class Command(BaseCommand):
    help = "Load communes and the travel-time matrix, and derive the covered area."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--matrix", required=True, help="data/travel-times.json")
        parser.add_argument("--communes", required=True, help="data/communes.geojson")

    def handle(self, *args: Any, matrix: str, communes: str, **options: Any) -> None:
        data = _read_json(matrix)
        by_insee = _communes(_read_json(communes))
        order = _validate_matrix(data, set(by_insee))
        origin = order.index(data["origin_for_area"])
        cap = int(data["area_minutes"])
        covered = [
            insee for insee, m in zip(order, data["minutes"][origin])
            if insee == data["origin_for_area"] or (m is not None and m <= cap)
        ]
        covered_idx = [order.index(i) for i in covered]

        with transaction.atomic():
            # Check foreign keys now rather than at commit, so a reference to a removed
            # commune surfaces here, as the error below.
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            TravelTime.objects.all().delete()
            CoveredArea.objects.all().delete()
            MatrixInfo.objects.all().delete()
            removed = Commune.objects.exclude(insee__in=by_insee)
            removed_codes = sorted(removed.values_list("insee", flat=True))
            try:
                with transaction.atomic():
                    removed.delete()
            except (ProtectedError, IntegrityError) as e:
                raise CommandError(
                    f"communes still referenced, not removed: {', '.join(removed_codes)} ({e})"
                ) from e
            Commune.objects.bulk_create(
                by_insee.values(),
                update_conflicts=True,
                unique_fields=["insee"],
                update_fields=["name", "chef_lieu", "geom", "postal_codes"],
            )
            CoveredArea.objects.bulk_create(CoveredArea(insee_id=i) for i in covered)
            TravelTime.objects.bulk_create(
                TravelTime(origin_id=order[i], destination_id=order[j], minutes=data["minutes"][i][j])
                for i in covered_idx
                for j in covered_idx
            )
            MatrixInfo.objects.create(
                version=data["version"], origin_for_area=data["origin_for_area"], area_minutes=cap
            )
        self.stdout.write(f"load_geo: {len(by_insee)} communes, {len(covered)} covered, version {data['version']}")
