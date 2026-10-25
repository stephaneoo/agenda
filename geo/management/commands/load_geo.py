"""Load communes and the travel-time matrix, and derive the covered area.

    manage.py load_geo --matrix data/travel-times.json --communes data/communes.geojson

Replaces whatever was loaded before, in one transaction.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Point
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from geo.models import Commune, CoveredArea, MatrixInfo, TravelTime


def _read_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CommandError(f"{path}: {e}") from e


def _communes(geojson: dict) -> dict[str, Commune]:
    communes: dict[str, Commune] = {}
    for feature in geojson.get("features", []):
        p = feature["properties"]
        geom = GEOSGeometry(json.dumps(feature["geometry"]), srid=4326)
        if geom.geom_type == "Polygon":
            geom = MultiPolygon(geom, srid=4326)
        if geom.geom_type != "MultiPolygon":
            raise CommandError(f"commune {p['insee']}: unexpected geometry {geom.geom_type}")
        communes[p["insee"]] = Commune(
            insee=p["insee"],
            name=p["name"],
            chef_lieu=Point(float(p["lon"]), float(p["lat"]), srid=4326),
            geom=geom,
            postal_codes=sorted(p.get("postal_codes") or []),
        )
    return communes


def _validate_matrix(matrix: dict, known: set[str]) -> list[str]:
    for key in ("version", "origin_for_area", "area_minutes", "communes", "minutes"):
        if key not in matrix:
            raise CommandError(f"matrix: missing {key}")
    order = [c["insee"] for c in matrix["communes"]]
    n = len(order)
    if len(set(order)) != n:
        raise CommandError("matrix: duplicate communes")
    if len(matrix["minutes"]) != n or any(len(row) != n for row in matrix["minutes"]):
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
            TravelTime.objects.all().delete()
            CoveredArea.objects.all().delete()
            MatrixInfo.objects.all().delete()
            Commune.objects.exclude(insee__in=by_insee).delete()
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
