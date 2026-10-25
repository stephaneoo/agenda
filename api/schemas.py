"""Response schemas of the public API v1. They document the API in OpenAPI; the values
come from agenda.read, which is the only thing the endpoints call."""

from __future__ import annotations

from ninja import Schema
from pydantic import Field


class Location(Schema):
    name: str | None
    street: str | None
    postal_code: str
    commune: str = Field(description="INSEE code")
    commune_name: str
    lat: float | None
    lon: float | None


class Organizer(Schema):
    name: str
    url: str | None


class Offers(Schema):
    is_free: bool | None
    price: str | None
    url: str | None


class SourceRef(Schema):
    name: str
    url: str | None
    licence: str
    updated_at: str = Field(description="The source's last update of this record, local date-time")


class Occurrence(Schema):
    id: str
    url: str = Field(description="Absolute URL of the public page")
    event_id: str
    name: str
    description: str | None
    start: str = Field(description="Local date-time (RFC 3339, Europe/Paris offset), or a local date YYYY-MM-DD")
    end: str | None = Field(description="Same format as start; null means no end to display")
    timezone: str = Field(description='Always "Europe/Paris"')
    status: str = Field(description="scheduled | rescheduled | postponed | cancelled")
    previous_start: str | None = Field(description="Set when status is rescheduled")
    category: str
    location: Location
    organizer: Organizer | None
    offers: Offers | None
    sources: list[SourceRef]
    updated_at: str


class OccurrencePage(Schema):
    items: list[Occurrence]
    next: str | None = Field(description="Opaque cursor of the next page; null on the last page")


class Event(Schema):
    id: str
    name: str
    description: str | None
    category: str
    location: Location
    organizer: Organizer | None
    offers: Offers | None
    sources: list[SourceRef]
    updated_at: str
    occurrences: list[Occurrence] = Field(description="Published occurrences from today onwards")


class Commune(Schema):
    insee: str
    name: str
    lat: float
    lon: float


class TravelTimes(Schema):
    version: str
    communes: list[str]
    minutes: list[list[int | None]] = Field(description="minutes[i][j]: from communes[i] to communes[j]; null: unreachable")


class Category(Schema):
    slug: str
    label: str


class SourceStatus(Schema):
    name: str
    last_success: str | None


class Status(Schema):
    last_sync: str | None
    sources: list[SourceStatus]


class Problem(Schema):
    """RFC 9457 problem details, served as application/problem+json."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str
