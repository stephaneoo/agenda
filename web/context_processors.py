from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpRequest

from agenda import read


def sync(request: HttpRequest) -> dict[str, Any]:
    """The last sync, for the footer of every page (spec: no stale data that looks current)."""
    return {"sync": read.sync_status()}


def site_origin(request: HttpRequest) -> str:
    """The origin the service worker may register on: AGENDA_SITE_URL's; unset, the request's
    own in dev mode, else none (production always sets it)."""
    if settings.SITE_URL:
        parts = urlsplit(settings.SITE_URL)
        return f"{parts.scheme}://{parts.netloc}"
    return f"{request.scheme}://{request.get_host()}" if settings.DEV else ""


def offline(request: HttpRequest) -> dict[str, Any]:
    """For app.js: where it may register the service worker, and the kill switch."""
    return {"site_origin": site_origin(request), "sw_kill": settings.SW_KILL}
