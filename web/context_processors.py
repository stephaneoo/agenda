from typing import Any

from django.http import HttpRequest

from agenda import read


def sync(request: HttpRequest) -> dict[str, Any]:
    """The last sync, for the footer of every page (spec: no stale data that looks current)."""
    return {"sync": read.sync_status()}
