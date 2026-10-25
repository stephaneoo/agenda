"""Read side of the geo app: covered area, travel times, commune resolution."""

from __future__ import annotations

import math
import re
import unicodedata

from django.contrib.gis.geos import Point
from django.db.models import QuerySet

from geo.models import Commune, CoveredArea, MatrixInfo, TravelTime


def covered_communes() -> QuerySet[Commune]:
    return Commune.objects.filter(coveredarea__isnull=False).order_by("insee")


def travel_minutes(origin: str, dest: str) -> int | None:
    return TravelTime.objects.filter(origin_id=origin, destination_id=dest).values_list("minutes", flat=True).first()


def reachable(origin: str, within: int) -> set[str]:
    """Covered communes at most `within` minutes from origin. The origin is always included.

    0 means the origin alone (spec), even if the matrix rounds a neighbour to 0 minutes.
    """
    if within == 0:
        return {origin}
    dests = TravelTime.objects.filter(origin_id=origin, minutes__lte=within).values_list("destination_id", flat=True)
    return {origin, *dests}


def normalize_name(name: str) -> str:
    """'Saint-Maurice-sur-Eygues', 'ST MAURICE SUR EYGUES' and 'saint maurice sur eygues' all match,
    and so do 'Les Pilles' and the sorted-list form 'Pilles (Les)'."""
    if m := re.fullmatch(r"\s*(.+?)\s*\(\s*([^()]+?)\s*\)\s*", name):
        name = f"{m[2]} {m[1]}"
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold()
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    return " ".join({"st": "saint", "ste": "sainte"}.get(w, w) for w in words)


def _valid_point(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0)


def _match_name(candidates: list[Commune], postal_code: str | None, name: str | None) -> str | None:
    if not postal_code or not name:
        return None
    wanted = normalize_name(name)
    hits = [c for c in candidates if postal_code in c.postal_codes and normalize_name(c.name) == wanted]
    return hits[0].insee if len(hits) == 1 else None


def resolve_commune(lat: float | None, lon: float | None, postal_code: str | None, name: str | None) -> str | None:
    """The INSEE code of the commune a location is in, or None when it can't be told.

    A valid point decides, by the commune it lands in. One outside every loaded commune
    gives None: the neighbours of the area are loaded, so such a point is outside the area
    or wrong, and is not guessed. Only when the point is missing or invalid do postcode and
    name have to match exactly one commune. A postcode alone never decides: several
    communes share one.
    """
    if _valid_point(lat, lon):
        hits = list(Commune.objects.filter(geom__intersects=Point(lon, lat, srid=4326)))
        if len(hits) == 1:
            return hits[0].insee
        # On a shared border the address breaks the tie, or nothing does.
        return _match_name(hits, postal_code, name)
    if not postal_code:
        return None
    return _match_name(list(Commune.objects.filter(postal_codes__contains=[postal_code])), postal_code, name)


def matrix_payload() -> dict:
    """The covered area's matrix, as served by /api/v1/travel-times."""
    info = MatrixInfo.objects.first()
    if info is None:
        return {"version": None, "communes": [], "minutes": []}
    communes = list(CoveredArea.objects.order_by("insee").values_list("insee", flat=True))
    index = {insee: i for i, insee in enumerate(communes)}
    minutes: list[list[int | None]] = [[None] * len(communes) for _ in communes]
    for origin, dest, value in TravelTime.objects.values_list("origin_id", "destination_id", "minutes"):
        if origin in index and dest in index:
            minutes[index[origin]][index[dest]] = value
    return {"version": info.version, "communes": communes, "minutes": minutes}
