"""The required-field check of the spec's Occurrence page JSON-LD section."""

from __future__ import annotations

import json
import re
from typing import Any

SCRIPT = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
STATUSES = {f"https://schema.org/Event{s}" for s in ("Scheduled", "Rescheduled", "Postponed", "Cancelled")}
LOCAL = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}\+0[12]:00)?$")


def jsonld_problems(data: Any) -> list[str]:
    p = []
    if not isinstance(data, dict) or data.get("@type") != "Event" or data.get("@context") != "https://schema.org":
        return ["not one schema.org Event"]
    if not data.get("name"):
        p.append("name")
    if not LOCAL.match(str(data.get("startDate", ""))):
        p.append("startDate")
    if "endDate" in data and not LOCAL.match(str(data["endDate"])):
        p.append("endDate")
    if data.get("eventStatus") not in STATUSES:
        p.append("eventStatus")
    if data.get("eventStatus", "").endswith("Rescheduled") and not data.get("previousStartDate"):
        p.append("previousStartDate")
    if data.get("eventAttendanceMode") != "https://schema.org/OfflineEventAttendanceMode":
        p.append("eventAttendanceMode")
    loc = data.get("location") or {}
    addr = loc.get("address") or {}
    if loc.get("@type") != "Place" or not loc.get("name"):
        p.append("location")
    if addr.get("@type") != "PostalAddress" or not addr.get("addressLocality") or not addr.get("postalCode") \
            or addr.get("addressCountry") != "FR":
        p.append("location.address")
    if "image" in data:
        p.append("image")
    return p


def page_jsonld(html: str) -> dict:
    """The page's JSON-LD; fails unless there is exactly one block and it passes the check."""
    blocks = SCRIPT.findall(html)
    assert len(blocks) == 1, f"{len(blocks)} ld+json blocks"
    data = json.loads(blocks[0])
    assert jsonld_problems(data) == []
    return data
