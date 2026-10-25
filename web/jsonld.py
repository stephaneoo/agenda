"""schema.org Event JSON-LD of an occurrence page (spec, Occurrence page JSON-LD)."""

from __future__ import annotations

import json
from typing import Any

from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe

from agenda.models import Occurrence

SCHEMA = "https://schema.org/"
STATUS = {
    "scheduled": SCHEMA + "EventScheduled",
    "rescheduled": SCHEMA + "EventRescheduled",
    "postponed": SCHEMA + "EventPostponed",
    "cancelled": SCHEMA + "EventCancelled",
}


def occurrence_jsonld(occ: Occurrence) -> dict[str, Any]:
    """One schema.org Event. No image in v1: images carry their own rights."""
    event = occ.event
    loc = event.location
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": event.name,
        "startDate": occ.start,
    }
    if occ.end:
        data["endDate"] = occ.end
    data["eventStatus"] = STATUS[occ.status]
    if occ.status == "rescheduled" and occ.previous_start:
        data["previousStartDate"] = occ.previous_start
    data["eventAttendanceMode"] = SCHEMA + "OfflineEventAttendanceMode"

    address: dict[str, Any] = {"@type": "PostalAddress"}
    if loc.get("street"):
        address["streetAddress"] = loc["street"]
    address |= {"addressLocality": loc["commune_name"], "postalCode": loc["postal_code"], "addressCountry": "FR"}
    place: dict[str, Any] = {"@type": "Place", "name": loc.get("name") or loc["commune_name"], "address": address}
    if loc.get("lat") is not None and loc.get("lon") is not None:
        place["geo"] = {"@type": "GeoCoordinates", "latitude": loc["lat"], "longitude": loc["lon"]}
    data["location"] = place

    if event.description:
        data["description"] = event.description
    if event.organizer:
        organizer = {"@type": "Organization", "name": event.organizer["name"]}
        if event.organizer.get("url"):
            organizer["url"] = event.organizer["url"]
        data["organizer"] = organizer
    offers = event.offers or {}
    offer: dict[str, Any] = {"@type": "Offer"}
    if offers.get("url"):
        offer["url"] = offers["url"]
    if offers.get("is_free") is True:
        offer |= {"price": 0, "priceCurrency": "EUR"}
    if len(offer) > 1:
        data["offers"] = offer
    if offers.get("is_free") is not None:
        data["isAccessibleForFree"] = offers["is_free"]
    return data


# What django.utils.html.json_script escapes: nothing in the text can close the script
# element or open a comment, whatever the source sent.
_ESCAPES = {ord("<"): "\\u003C", ord(">"): "\\u003E", ord("&"): "\\u0026"}


def ldjson_script(data: Any) -> SafeString:
    """A <script type="application/ld+json"> element, safe for untrusted text."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")).translate(_ESCAPES)
    return format_html('<script type="application/ld+json">{}</script>', mark_safe(text))
