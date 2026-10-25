"""Public read API v1 (spec, Specification). Every endpoint calls agenda.read and nothing else
for events; the reference endpoints read geo and the category list."""

from __future__ import annotations

from datetime import date

from typing import Any

from django.http import HttpRequest, HttpResponse, HttpResponsePermanentRedirect
from ninja import NinjaAPI, Query
from ninja.errors import HttpError, ValidationError

from agenda import read
from agenda.categories import SLUGS
from agenda.timeutil import format_local
from agenda_site.site import base_url
from api import schemas
from api.problems import problem
from geo.services import covered_communes, matrix_payload

VERSION = "1.2.0"  # bump on every change to the API (spec, Versioning); tools.check_openapi_version checks it
DATE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"


class AgendaAPI(NinjaAPI):
    def get_openapi_schema(self, *args: Any, **kwargs: Any) -> Any:
        """Error responses are application/problem+json, not application/json."""
        schema = super().get_openapi_schema(*args, **kwargs)
        for operations in schema["paths"].values():
            for operation in operations.values():
                for code, response in operation.get("responses", {}).items():
                    content = response.get("content", {})
                    if int(code) >= 400 and "application/json" in content:
                        response["content"] = {"application/problem+json": content["application/json"]}
        return schema


api = AgendaAPI(
    title="Agenda — API publique",
    version=VERSION,
    description="Read-only API of the events agenda around Nyons. Data: Licence Ouverte 2.0; "
    "each occurrence cites its sources and their last update date. Public and cookieless: every "
    "response to a GET carries `Access-Control-Allow-Origin: *`, and OPTIONS answers CORS "
    "preflights with 204.",
    docs_url=None,
    urls_namespace="api-v1",
)

ERRORS = {404: schemas.Problem, 410: schemas.Problem}
INSEE = r"^[0-9][0-9AB][0-9]{3}$"
# One body for every 410: it must not tell a moderator's hide from a source's withdrawal.
GONE = "This resource is no longer available."


def _date(value: str | None, name: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise read.InvalidQuery(f"{name} is not a date (YYYY-MM-DD)") from None


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
    commune: str | None = Query(None, pattern=INSEE, max_length=5,
                                description="INSEE code of the origin, in the covered area. Required with "
                                "`within`; without `within`, only occurrences in that commune itself are returned."),
    within: int | None = Query(None, ge=0, le=read.MAX_WITHIN,
                               description="Minutes by car from `commune`, 0 to 60. 0 keeps the origin commune only."),
    date_from: str | None = Query(None, alias="from", pattern=DATE, format="date",
                                  description="Local date YYYY-MM-DD, inclusive; default today in Europe/Paris"),
    date_to: str | None = Query(None, alias="to", pattern=DATE, format="date",
                                description="Local date YYYY-MM-DD, inclusive; default from + 30 days; "
                                f"at most {read.MAX_SPAN_DAYS} days after from"),
    category: list[schemas.CategorySlug] = Query([], description="Category slugs, repeated"),
    cursor: str | None = Query(None, max_length=read.MAX_CURSOR, description="The `next` of the previous page"),
    limit: int = Query(read.DEFAULT_LIMIT, ge=1, le=read.MAX_LIMIT, description=f"1 to {read.MAX_LIMIT}"),
):
    page = read.search_occurrences(
        commune, within, _date(date_from, "from"), _date(date_to, "to"), list(category), cursor, limit
    )
    root = base_url(request)
    return {"items": [read.occurrence_dict(o, root) for o in page.items], "next": page.next}


@api.get("/occurrences/{id}", response={200: schemas.Occurrence, 301: None, **ERRORS}, summary="One occurrence")
def occurrence(request: HttpRequest, id: str):
    found = read.get_occurrence(id)
    if isinstance(found, read.MovedTo):
        return HttpResponsePermanentRedirect(f"/api/v1/occurrences/{found.new_id}")
    if isinstance(found, read.Gone):
        return problem(410, GONE)
    return read.occurrence_dict(found, base_url(request))


@api.get("/events/{id}", response={200: schemas.Event, **ERRORS}, summary="One event, with its coming occurrences")
def event(request: HttpRequest, id: str):
    found = read.get_event(id)
    if isinstance(found, read.Gone):
        return problem(410, GONE)
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


@api.get("/status", response=schemas.SyncStatus, summary="Last sync of each source")
def status(request: HttpRequest):
    s = read.sync_status()
    fmt = lambda d: format_local(d) if d else None  # noqa: E731
    return {
        "last_sync": fmt(s["last_sync"]),
        "sources": [{"name": x["name"], "last_success": fmt(x["last_success"])} for x in s["sources"]],
    }
