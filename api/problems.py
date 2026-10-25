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
