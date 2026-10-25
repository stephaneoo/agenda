"""Public read API v1 (spec, Specification). Every endpoint calls agenda.read and nothing else
for events; the reference endpoints read geo and the category list."""

from __future__ import annotations

from datetime import date

from django.http import HttpRequest, HttpResponse, HttpResponsePermanentRedirect, JsonResponse
from ninja import NinjaAPI, Query
from ninja.errors import HttpError, ValidationError

from agenda import read
from agenda.categories import SLUGS
from agenda.timeutil import format_local
from agenda_site.site import base_url
from api import schemas
from geo.services import covered_communes, matrix_payload

VERSION = "1.0.0"  # bump on every change to the API (spec, Versioning)

api = NinjaAPI(
    title="Agenda — API publique",
    version=VERSION,
    description="Read-only API of the events agenda around Nyons. Data: Licence Ouverte 2.0; "
    "each occurrence cites its sources and their last update date.",
    docs_url=None,
    urls_namespace="api-v1",
)

TITLES = {404: "Not Found", 410: "Gone", 422: "Unprocessable Content"}
ERRORS = {404: schemas.Problem, 410: schemas.Problem, 422: schemas.Problem}


def problem(status: int, detail: str) -> JsonResponse:
    body = {"type": "about:blank", "title": TITLES.get(status, "Error"), "status": status, "detail": detail}
    return JsonResponse(body, status=status, content_type="application/problem+json",
                        json_dumps_params={"ensure_ascii": False})


@api.exception_handler(read.InvalidQuery)
def _invalid(request: HttpRequest, exc: read.InvalidQuery) -> HttpResponse:
    return problem(422, exc.detail)


@api.exception_handler(ValidationError)
def _validation(request: HttpRequest, exc: ValidationError) -> HttpResponse:
    parts = []
    for e in exc.errors:
        where = ".".join(str(p) for p in e.get("loc", ()) if p not in ("query", "path"))
        parts.append(f"{where}: {e.get('msg', 'invalid')}" if where else str(e.get("msg", "invalid")))
    return problem(422, "; ".join(parts) or "invalid request")


@api.exception_handler(read.NotFound)
def _not_found(request: HttpRequest, exc: read.NotFound) -> HttpResponse:
    return problem(404, "no such id")


@api.exception_handler(HttpError)
def _http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
    return problem(exc.status_code, str(exc))


@api.get("/occurrences", response={200: schemas.OccurrencePage, 422: schemas.Problem}, summary="Search occurrences")
def occurrences(
    request: HttpRequest,
    commune: str | None = Query(None, description="INSEE code of the origin; required with within"),
    within: int | None = Query(None, description="Minutes by car from commune, 0 to 60"),
    date_from: date | None = Query(None, alias="from", description="Local date, inclusive; default today"),
    date_to: date | None = Query(None, alias="to", description="Local date, inclusive; default from + 30 days"),
    category: list[str] = Query([], description="Category slugs, repeated"),
    cursor: str | None = Query(None, description="The `next` of the previous page"),
    limit: int = Query(read.DEFAULT_LIMIT, description=f"1 to {read.MAX_LIMIT}"),
):
    page = read.search_occurrences(commune, within, date_from, date_to, category, cursor, limit)
    root = base_url(request)
    return {"items": [read.occurrence_dict(o, root) for o in page.items], "next": page.next}


@api.get("/occurrences/{id}", response={200: schemas.Occurrence, 301: None, **ERRORS}, summary="One occurrence")
def occurrence(request: HttpRequest, id: str):
    found = read.get_occurrence(id)
    if isinstance(found, read.MovedTo):
        return HttpResponsePermanentRedirect(f"/api/v1/occurrences/{found.new_id}")
    if isinstance(found, read.Gone):
        return problem(410, "this occurrence is hidden or withdrawn")
    return read.occurrence_dict(found, base_url(request))


@api.get("/events/{id}", response={200: schemas.Event, **ERRORS}, summary="One event, with its coming occurrences")
def event(request: HttpRequest, id: str):
    found = read.get_event(id)
    if isinstance(found, read.Gone):
        return problem(410, "this event is hidden")
    return read.event_dict(found, base_url(request))


@api.get("/communes", response=list[schemas.Commune], summary="Covered communes")
def communes(request: HttpRequest):
    return [
        {"insee": c.insee, "name": c.name, "lat": c.chef_lieu.y, "lon": c.chef_lieu.x} for c in covered_communes()
    ]


@api.get("/travel-times", response=schemas.TravelTimes, summary="Travel-time matrix of the covered area")
def travel_times(request: HttpRequest):
    payload = matrix_payload()
    return {**payload, "version": payload["version"] or ""}


@api.get("/categories", response=list[schemas.Category], summary="Category slugs and labels")
def categories(request: HttpRequest):
    return [{"slug": slug, "label": label} for slug, label in SLUGS.items()]


@api.get("/status", response=schemas.Status, summary="Last sync of each source")
def status(request: HttpRequest):
    s = read.sync_status()
    fmt = lambda d: format_local(d) if d else None  # noqa: E731
    return {
        "last_sync": fmt(s["last_sync"]),
        "sources": [{"name": x["name"], "last_success": fmt(x["last_success"])} for x in s["sources"]],
    }
