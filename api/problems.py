"""RFC 9457 problem details: every API error is application/problem+json."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus

from django.http import HttpRequest, HttpResponse, JsonResponse

PREFIX = "/api/"


def problem(status: int, detail: str) -> JsonResponse:
    try:
        title = HTTPStatus(status).phrase
    except ValueError:
        title = "Error"
    body = {"type": "about:blank", "title": title, "status": status, "detail": detail}
    return JsonResponse(body, status=status, content_type="application/problem+json",
                        json_dumps_params={"ensure_ascii": False})


def not_found(request: HttpRequest, *args: object, **kwargs: object) -> JsonResponse:
    """Catch-all under /api/v1/: unknown paths, trailing slashes."""
    return problem(404, f"no such endpoint: {request.path}")


class ProblemErrorsMiddleware:
    """Turns any other error response under /api/ (e.g. django-ninja's 405) into a problem."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if (
            request.path.startswith(PREFIX)
            and response.status_code >= 400
            and not response.get("Content-Type", "").startswith("application/problem+json")
        ):
            replaced = problem(response.status_code, response.reason_phrase or "error")
            if response.has_header("Allow"):
                replaced["Allow"] = response["Allow"]
            return replaced
        return response


V1 = "/api/v1/"
CORS_MAX_AGE = 86400


class CorsMiddleware:
    """The read API is public, read-only and cookieless: any origin may read it.

    GET and HEAD responses under /api/v1/, problems included, allow every origin. OPTIONS
    there is a CORS preflight, answered with 204. Never with credentials.
    Placed before ProblemErrorsMiddleware, so it sees the final response.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not request.path.startswith(V1):
            return self.get_response(request)
        if request.method == "OPTIONS":
            response = HttpResponse(status=204)
            response["Access-Control-Allow-Origin"] = "*"
            response["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
            response["Access-Control-Max-Age"] = str(CORS_MAX_AGE)
            if headers := request.headers.get("Access-Control-Request-Headers"):
                # Without credentials, any header is fine to read with: echo what was asked.
                response["Access-Control-Allow-Headers"] = headers[:500]
            return response
        response = self.get_response(request)
        if request.method in ("GET", "HEAD"):
            response["Access-Control-Allow-Origin"] = "*"
        return response
