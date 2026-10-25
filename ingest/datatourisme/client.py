"""The data.gouv.fr tabular API over DATAtourisme's simplified events export.

    https://tabular-api.data.gouv.fr/api/resources/<RESOURCE>/data/?<filters>&page=N&page_size=M

Each query is paged by page number, sorted by the API's row id so pages don't overlap. Any
failed page raises FetchError: a non-200 answer (after the session's retries), a network
error, a body that isn't the expected JSON, or a total that moves between pages (the export
being replaced mid-run, which could shift rows from one page to another and lose some). The
caller must then treat the run as incomplete: a missing row is not a deleted record.
Page 1 is fetched again at the end of each query, and the sync compares the export's
version (source_version) before and after: the same total can hide a replaced export.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RESOURCE = "2c6023f2-ffce-4c1a-9728-bdc020e463b5"
URL = f"https://tabular-api.data.gouv.fr/api/resources/{RESOURCE}/data/"
# The export's version, checked before and after a run: the resource's last_modified on
# data.gouv.fr, and when the tabular API last loaded it.
RESOURCE_META_URL = f"https://www.data.gouv.fr/api/2/datasets/resources/{RESOURCE}/"
TABULAR_META_URL = f"https://tabular-api.data.gouv.fr/api/resources/{RESOURCE}/"
PAGE_SIZE = 200
TIMEOUT = 30  # seconds, per request
MAX_PAGES = 200  # per query: 40,000 rows, far more than the area has
MAX_BODY = 20 * 1024 * 1024
USER_AGENT = "agenda-nyons-sync/1 (+https://example.org/a-propos/)"


class FetchError(Exception):
    """A page could not be fetched: the run is incomplete."""


def make_session() -> requests.Session:
    """A session that retries connection errors, 429 and 5xx, with backoff (2, 4, 8, 16 s)."""
    retry = Retry(
        total=4, connect=4, read=4, status=4, backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True, raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def _get_json(session: Any, url: str, params: dict[str, Any] | None, what: str) -> Any:
    try:
        response = session.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise FetchError(f"{what}: {type(e).__name__}: {e}") from e
    if response.status_code != 200:
        raise FetchError(f"{what}: HTTP {response.status_code}")
    if len(response.content) > MAX_BODY:
        raise FetchError(f"{what}: body over {MAX_BODY} bytes")
    try:
        return response.json()
    except ValueError as e:
        raise FetchError(f"{what}: not JSON") from e


def source_version(session: Any) -> tuple[str, str]:
    """(resource last_modified, tabular created_at). A run whose version differs at the end
    from the start read two exports: it is incomplete."""
    meta = _get_json(session, RESOURCE_META_URL, None, "resource metadata")
    tabular = _get_json(session, TABULAR_META_URL, None, "tabular metadata")
    try:
        modified, created = meta["resource"]["last_modified"], tabular["created_at"]
    except (KeyError, TypeError) as e:
        raise FetchError("export metadata: unexpected body") from e
    if not isinstance(modified, str) or not isinstance(created, str):
        raise FetchError("export metadata: unexpected body")
    return modified, created


def _page(session: Any, params: dict[str, Any]) -> dict:
    try:
        response = session.get(URL, params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise FetchError(f"page {params.get('page')}: {type(e).__name__}: {e}") from e
    if response.status_code != 200:
        raise FetchError(f"page {params.get('page')}: HTTP {response.status_code}")
    if len(response.content) > MAX_BODY:
        raise FetchError(f"page {params.get('page')}: body over {MAX_BODY} bytes")
    try:
        body = response.json()
    except ValueError as e:
        raise FetchError(f"page {params.get('page')}: not JSON") from e
    if (
        not isinstance(body, dict) or not isinstance(body.get("data"), list)
        or not isinstance(body.get("meta"), dict) or not isinstance(body["meta"].get("total"), int)
        or not all(isinstance(r, dict) for r in body["data"])
    ):
        raise FetchError(f"page {params.get('page')}: unexpected body")
    return body


def _ends(rows: list[dict]) -> tuple[Any, Any]:
    return (rows[0].get("__id"), rows[-1].get("__id")) if rows else (None, None)


def iter_query(session: Any, filters: dict[str, Any], page_size: int = PAGE_SIZE) -> Iterator[dict]:
    """Every row matching `filters`, page after page. At the end page 1 is fetched again:
    a different total or first and last row id means the rows moved under the pagination
    (the same total can hide a deletion and an addition), and raises FetchError."""
    total: int | None = None
    seen = 0
    first: tuple[Any, Any] | None = None
    params = {**filters, "__id__sort": "asc", "page_size": page_size}
    for page in range(1, MAX_PAGES + 1):
        body = _page(session, {**params, "page": page})
        if first is None:
            first = _ends(body["data"])
        if total is None:
            total = body["meta"]["total"]
        elif body["meta"]["total"] != total:
            raise FetchError(f"page {page}: total moved from {total} to {body['meta']['total']} during the run")
        rows = body["data"]
        # The server's own page size (it may cap the one asked for).
        size = body["meta"].get("page_size")
        size = size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else page_size
        expected = min(size, total - seen)
        if len(rows) != expected:
            raise FetchError(f"page {page}: {len(rows)} rows, expected {expected}")
        yield from rows
        seen += len(rows)
        if seen >= total:
            again = _page(session, {**params, "page": 1})
            if again["meta"]["total"] != total or _ends(again["data"]) != first:
                raise FetchError("the export changed during the run (page 1 differs at the end)")
            return
    raise FetchError(f"more than {MAX_PAGES} pages ({total} rows)")


def iter_rows(session: Any, queries: Iterable[dict[str, Any]], page_size: int = PAGE_SIZE) -> Iterator[dict]:
    """Every row of every query, in order. The same row may come from several queries."""
    for filters in queries:
        yield from iter_query(session, filters, page_size)
