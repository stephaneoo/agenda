"""Content-Security-Policy on every response.

The public pages load only their own files: no inline script or style (the JSON-LD block is
data, which CSP doesn't execute), no frames, no plugins. worker-src and manifest-src are for
the service worker and the web manifest of Task 12.

The admin gets its own policy. Django 5.2's admin loads every script from a file (its
constants travel in data- attributes), so script-src stays 'self'; its widgets and some
templates set inline styles, hence style-src 'unsafe-inline', and its CSS uses data: images.
"""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

POLICY = "; ".join([
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self'",
    "connect-src 'self'",
    "worker-src 'self'",
    "manifest-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
])

ADMIN_POLICY = "; ".join([
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
])

ADMIN_PREFIX = "/admin/"


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if not response.has_header("Content-Security-Policy"):
            response["Content-Security-Policy"] = ADMIN_POLICY if request.path.startswith(ADMIN_PREFIX) else POLICY
        return response
