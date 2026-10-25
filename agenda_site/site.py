"""The site's absolute root URL, for the `url` of occurrences."""

from django.conf import settings
from django.http import HttpRequest


def base_url(request: HttpRequest) -> str:
    """AGENDA_SITE_URL when set (canonical URLs once a domain exists), else the request's own root."""
    return settings.SITE_URL or request.build_absolute_uri("/")
