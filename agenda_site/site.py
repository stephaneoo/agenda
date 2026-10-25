"""The site's absolute root URL, the peer address, and the loopback-only admin."""

from collections.abc import Callable

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse

# Only a request from the host itself. Caddy reaches gunicorn over a Unix socket, where
# gunicorn sets REMOTE_ADDR to "" (no peer address), so a request through Caddy is never one
# of these, whatever headers it carries. On the host, gunicorn also listens on loopback, for
# the deploy's check and the admin's SSH tunnel (plan, Tasks 14 and 16).
LOOPBACK = frozenset({"127.0.0.1", "::1"})


def is_loopback(request: HttpRequest) -> bool:
    return request.META.get("REMOTE_ADDR") in LOOPBACK


def base_url(request: HttpRequest) -> str:
    """AGENDA_SITE_URL when set (canonical URLs once a domain exists), else the request's own root."""
    return settings.SITE_URL or request.build_absolute_uri("/")


def client_ip(request: HttpRequest) -> str:
    """The peer address for login throttling: REMOTE_ADDR, never X-Forwarded-For."""
    return request.META.get("REMOTE_ADDR") or ""


class LoopbackAdminMiddleware:
    """/admin/ (the moderation pages included) is a 404 unless REMOTE_ADDR is loopback, in dev
    mode too. Defence in depth: Caddy never proxies /admin/ (plan, Task 14)."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path_info
        if (path == "/admin" or path.startswith("/admin/")) and not is_loopback(request):
            raise Http404
        return self.get_response(request)
