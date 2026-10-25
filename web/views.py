"""Server-rendered pages. Everything they show comes from agenda.read."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from django.http import Http404, HttpRequest, HttpResponse, HttpResponsePermanentRedirect, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from agenda import read
from agenda.categories import SLUGS
from agenda.models import Occurrence
from agenda.timeutil import PARIS, parse, today
from web.jsonld import occurrence_jsonld

RADII = [0, 10, 20, 30]
DEFAULT_RADIUS = 30
PERIODS = {"aujourdhui": "Aujourd'hui", "week-end": "Ce week-end", "7j": "7 jours", "30j": "30 jours"}
DEFAULT_PERIOD = "7j"
STATUS_LABELS = {"rescheduled": "Reprogrammé", "postponed": "Reporté", "cancelled": "Annulé"}
PAGE_SIZE = read.MAX_LIMIT
MAX_PAGES = 5  # 1000 occurrences: more than the area has in 30 days; beyond, the page says so


def now() -> datetime:
    return timezone.now()


def period_range(period: str, day: date) -> tuple[date, date]:
    if period == "aujourdhui":
        return day, day
    if period == "week-end":
        saturday = day + timedelta(days=(5 - day.weekday()) % 7) if day.weekday() < 5 else day
        return saturday, day + timedelta(days=6 - day.weekday())
    return day, day + timedelta(days=29 if period == "30j" else 6)


@dataclass
class Row:
    occ: Occurrence
    start: date | datetime
    end: date | datetime | None
    has_time: bool
    end_has_time: bool
    status_label: str | None
    category_label: str


@dataclass
class Day:
    day: date
    rows: list[Row] = field(default_factory=list)


def _row(occ: Occurrence) -> Row:
    start = parse(occ.start)
    end = parse(occ.end) if occ.end else None
    return Row(
        occ, start, end, isinstance(start, datetime), isinstance(end, datetime),
        STATUS_LABELS.get(occ.status), SLUGS.get(occ.event.category, occ.event.category),
    )


def _wall(value: str, is_end: bool) -> datetime:
    """Local wall-clock time, naive. A date starts at 00:00; as an end, it runs to 24:00."""
    v = parse(value)
    if isinstance(v, datetime):
        return v.astimezone(PARIS).replace(tzinfo=None)
    return datetime.combine(v, time()) + (timedelta(days=1) if is_end else timedelta())


def _spanning(occ: Occurrence) -> bool:
    """More than 24 h on the wall clock: shown once under "En cours", not under each day.
    Measured in local time, so a whole day on a DST change (23 or 25 h) is still one day."""
    start = _wall(occ.start, False)
    if occ.end:
        end = _wall(occ.end, True)
    else:
        end = _wall(occ.start, True) if len(occ.start) == 10 else start
    return end - start > timedelta(hours=24)


@require_safe
def agenda(request: HttpRequest) -> HttpResponse:
    communes = read.area_communes()
    known = {insee for insee, _ in communes}
    commune = request.GET.get("commune", "")
    commune = commune if commune in known else read.default_origin()
    try:
        radius = int(request.GET.get("rayon", DEFAULT_RADIUS))
    except ValueError:
        radius = DEFAULT_RADIUS
    radius = radius if radius in RADII else DEFAULT_RADIUS
    period = request.GET.get("periode", DEFAULT_PERIOD)
    period = period if period in PERIODS else DEFAULT_PERIOD
    categories = [c for c in request.GET.getlist("categorie") if c in SLUGS]
    date_from, date_to = period_range(period, today())

    items: list[Occurrence] = []
    truncated = False
    if commune in known:
        cursor = None
        for _ in range(MAX_PAGES):
            page = read.search_occurrences(commune, radius, date_from, date_to, categories, cursor, PAGE_SIZE)
            items += page.items
            if not (cursor := page.next):
                break
        truncated = cursor is not None

    ongoing, days = [], {}
    for occ in items:
        row = _row(occ)
        if _spanning(occ):
            ongoing.append(row)
        else:
            d = max(occ.start_utc.astimezone(timezone.get_current_timezone()).date(), date_from)
            days.setdefault(d, Day(d)).rows.append(row)
    return render(request, "web/agenda.html", {
        "communes": communes, "commune": commune, "radii": RADII, "radius": radius,
        "periods": PERIODS, "period": period, "categories": SLUGS, "selected_categories": categories,
        "ongoing": ongoing, "days": [days[d] for d in sorted(days)], "count": len(items),
        "truncated": truncated,
    })


@require_safe
def occurrence(request: HttpRequest, slug: str, day: str) -> HttpResponse:
    try:
        local_date = date.fromisoformat(day)
    except ValueError:
        raise Http404 from None
    url_key = slug.rsplit("-", 1)[-1]
    try:
        found = read.find_occurrence(url_key, local_date)
        if isinstance(found, read.MovedTo):
            target = read.get_occurrence(found.new_id)
            if isinstance(target, Occurrence):
                return HttpResponsePermanentRedirect(target.get_absolute_url())
            found = read.Gone(found.new_id)
    except read.NotFound:
        raise Http404 from None
    if isinstance(found, read.Gone):
        return render(request, "web/gone.html", status=410)
    if request.path != found.get_absolute_url():
        return HttpResponsePermanentRedirect(found.get_absolute_url())
    return render(request, "web/occurrence.html", {
        "row": _row(found), "occ": found, "event": found.event, "jsonld": occurrence_jsonld(found),
        "over": found.end_utc_inclusive < now(),
        "previous": parse(found.previous_start) if found.previous_start else None,
        "previous_has_time": bool(found.previous_start and len(found.previous_start) > 10),
        "sources": [{**s, "updated": parse(s["updated_at"])} for s in found.event.sources],
    })


@require_safe
def about(request: HttpRequest) -> HttpResponse:
    return render(request, "web/about.html")


def _migrations_applied() -> bool:
    """Every migration on disk is recorded as applied (not just the leaves: a gap counts)."""
    loader = MigrationExecutor(connection).loader
    return set(loader.graph.nodes) <= set(loader.applied_migrations)


@never_cache
@require_safe
def healthz(request: HttpRequest) -> JsonResponse:
    """200 iff the database answers and every migration is applied. `debug` lets the deploy
    refuse a production that runs with DEBUG on."""
    database = migrations = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        database = True
        migrations = _migrations_applied()
    except DatabaseError:
        pass
    ok = database and migrations
    body = {"status": "ok" if ok else "error", "database": database, "migrations": migrations, "debug": settings.DEBUG}
    return JsonResponse(body, status=200 if ok else 503)
