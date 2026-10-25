"""The data.gouv.fr tabular API over DATAtourisme's simplified events export.

    https://tabular-api.data.gouv.fr/api/resources/<RESOURCE>/data/?<filters>&page=N&page_size=M

Each query is paged by page number, sorted by the API's row id so pages don't overlap. Any
failed page raises FetchError: a non-200 answer (after the session's retries), a redirect, a
network error, a body that isn't the expected JSON or is over MAX_BODY, a run past its
deadline, or a total that moves between pages (the export being replaced mid-run, which could
shift rows from one page to another and lose some). The caller must then treat the run as
incomplete: a missing row is not a deleted record.
Page 1 is fetched again at the end of each query, and the sync compares the export's
version (source_version) before and after: the same total can hide a replaced export.

Hardening (batch C security review):
- make_session() only speaks https, to ALLOWED_HOSTS: any other scheme or host is refused
  before connecting. Redirects are never followed (a 3xx is a FetchError).
- Bodies are streamed: a Content-Length over MAX_BODY is refused unread, and the decoded
  bytes are counted as they arrive (gzip is inflated here, bounded), so a compression bomb
  stops at MAX_BODY.
- A `deadline` (time.monotonic()) is checked before each request and between reads: past
  it, FetchError. Retry-After is capped at MAX_RETRY_AFTER seconds.
- Every row is sanitised as it arrives (sanitise): no NUL, no lone surrogate, which
  PostgreSQL's jsonb and UTF-8 refuse.
"""

from __future__ import annotations

import json
import re
import time
import zlib
from collections.abc import Collection, Iterable, Iterator
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import BaseAdapter, HTTPAdapter
from urllib3.util.retry import Retry

RESOURCE = "2c6023f2-ffce-4c1a-9728-bdc020e463b5"
URL = f"https://tabular-api.data.gouv.fr/api/resources/{RESOURCE}/data/"
# The export's version, checked before and after a run: the resource's last_modified on
# data.gouv.fr, and when the tabular API last loaded it.
RESOURCE_META_URL = f"https://www.data.gouv.fr/api/2/datasets/resources/{RESOURCE}/"
TABULAR_META_URL = f"https://tabular-api.data.gouv.fr/api/resources/{RESOURCE}/"
ALLOWED_HOSTS = frozenset({"tabular-api.data.gouv.fr", "www.data.gouv.fr"})
PAGE_SIZE = 200
TIMEOUT = 30  # seconds, per connect and per read
MAX_PAGES = 200  # per query: 40,000 rows, far more than the area has
MAX_BODY = 20 * 1024 * 1024  # decoded bytes
MAX_RETRY_AFTER = 60  # seconds
CHUNK = 64 * 1024
USER_AGENT = "agenda-nyons-sync/1 (+https://example.org/a-propos/)"


class FetchError(Exception):
    """A page could not be fetched: the run is incomplete."""


# --- Session -----------------------------------------------------------------------------------

class CappedRetry(Retry):
    """Retry, with Retry-After capped at MAX_RETRY_AFTER: a server can't park the run."""

    def parse_retry_after(self, retry_after: str) -> float:
        return min(super().parse_retry_after(retry_after), MAX_RETRY_AFTER)


class RefusedURL(requests.exceptions.InvalidURL):
    """A request to a scheme or host the session doesn't allow: never sent."""


class _Refuse(BaseAdapter):
    def send(self, request: requests.PreparedRequest, *args: Any, **kwargs: Any) -> requests.Response:
        raise RefusedURL(f"refused: {urlsplit(request.url or '').scheme} is not allowed")

    def close(self) -> None:
        pass


class _AllowedHosts(HTTPAdapter):
    """HTTPAdapter that refuses any host outside `hosts`, before connecting."""

    def __init__(self, hosts: Collection[str], **kwargs: Any) -> None:
        self.hosts = frozenset(hosts)
        super().__init__(**kwargs)

    def send(self, request: requests.PreparedRequest, *args: Any, **kwargs: Any) -> requests.Response:
        host = urlsplit(request.url or "").hostname
        if host not in self.hosts:
            raise RefusedURL(f"refused: host {host!r} is not allowed")
        return super().send(request, *args, **kwargs)


def make_session(*, hosts: Collection[str] = ALLOWED_HOSTS, schemes: Collection[str] = ("https",)) -> requests.Session:
    """A session that retries connection errors, 429 and 5xx, with backoff (2, 4, 8, 16 s), and
    talks only to `hosts` over `schemes` (tests open plain http to a local server)."""
    retry = CappedRetry(
        total=4, connect=4, read=4, status=4, backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True, raise_on_status=False, redirect=0,
    )
    session = requests.Session()
    session.max_redirects = 0
    for scheme in ("https", "http"):
        adapter = _AllowedHosts(hosts, max_retries=retry) if scheme in schemes else _Refuse()
        session.mount(f"{scheme}://", adapter)
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"})
    return session


# --- Rows ----------------------------------------------------------------------------------------

_SURROGATE = re.compile("[\ud800-\udfff]")


def sanitise(value: Any) -> Any:
    """`value` with every string (keys too, at any depth) stripped of NUL and its lone
    surrogates replaced by U+FFFD: what jsonb and UTF-8 can store."""
    if isinstance(value, str):
        return _SURROGATE.sub("�", value.replace("\x00", ""))
    if isinstance(value, dict):
        return {sanitise(k): sanitise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitise(v) for v in value]
    return value


# --- Fetching ------------------------------------------------------------------------------------

def check_deadline(deadline: float | None, what: str) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise FetchError(f"{what}: the run is past its deadline")


def _read_body(response: Any, what: str, deadline: float | None) -> bytes:
    """The decoded body, at most MAX_BODY bytes, reading one network chunk at a time."""
    length = (response.headers.get("Content-Length") or "").strip()
    if length.isdigit() and int(length) > MAX_BODY:
        raise FetchError(f"{what}: body over {MAX_BODY} bytes (Content-Length {length})")
    encoding = (response.headers.get("Content-Encoding") or "identity").strip().lower()
    if encoding not in ("identity", "gzip"):
        raise FetchError(f"{what}: unexpected Content-Encoding {encoding[:40]!r}")
    inflate = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    out = bytearray()
    raw = response.raw
    while True:
        check_deadline(deadline, what)
        chunk = raw.read1(CHUNK, decode_content=False)
        if not chunk:
            break
        if inflate is None:
            out += chunk
        else:
            while chunk:
                # Never more than one byte past the cap per call (0 would mean unbounded).
                out += inflate.decompress(chunk, MAX_BODY + 1 - len(out))
                if len(out) > MAX_BODY:
                    break
                chunk = inflate.unconsumed_tail
        if len(out) > MAX_BODY:
            raise FetchError(f"{what}: body over {MAX_BODY} bytes")
    if inflate is not None:
        out += inflate.flush()
        if len(out) > MAX_BODY:
            raise FetchError(f"{what}: body over {MAX_BODY} bytes")
    return bytes(out)


def _get_json(session: Any, url: str, params: dict[str, Any] | None, what: str, deadline: float | None = None) -> Any:
    check_deadline(deadline, what)
    try:
        response = session.get(url, params=params, timeout=TIMEOUT, stream=True, allow_redirects=False)
    except requests.RequestException as e:
        raise FetchError(f"{what}: {type(e).__name__}: {e}") from e
    try:
        if 300 <= response.status_code < 400:
            raise FetchError(f"{what}: HTTP {response.status_code} redirect, not followed")
        if response.status_code != 200:
            raise FetchError(f"{what}: HTTP {response.status_code}")
        body = _read_body(response, what, deadline)
    except (requests.RequestException, OSError, zlib.error) as e:
        raise FetchError(f"{what}: {type(e).__name__}: {e}") from e
    finally:
        response.close()
    try:
        return json.loads(body)
    except (ValueError, RecursionError) as e:
        raise FetchError(f"{what}: not JSON") from e


def source_version(session: Any, deadline: float | None = None) -> tuple[str, str]:
    """(resource last_modified, tabular created_at). A run whose version differs at the end
    from the start read two exports: it is incomplete."""
    meta = _get_json(session, RESOURCE_META_URL, None, "resource metadata", deadline)
    tabular = _get_json(session, TABULAR_META_URL, None, "tabular metadata", deadline)
    try:
        modified, created = meta["resource"]["last_modified"], tabular["created_at"]
    except (KeyError, TypeError) as e:
        raise FetchError("export metadata: unexpected body") from e
    if not isinstance(modified, str) or not isinstance(created, str):
        raise FetchError("export metadata: unexpected body")
    return modified, created


def _page(session: Any, params: dict[str, Any], deadline: float | None = None) -> dict:
    what = f"page {params.get('page')}"
    body = _get_json(session, URL, params, what, deadline)
    if (
        not isinstance(body, dict) or not isinstance(body.get("data"), list)
        or not isinstance(body.get("meta"), dict) or not isinstance(body["meta"].get("total"), int)
        or not all(isinstance(r, dict) for r in body["data"])
    ):
        raise FetchError(f"{what}: unexpected body")
    try:
        body["data"] = [sanitise(r) for r in body["data"]]
    except RecursionError as e:
        raise FetchError(f"{what}: rows nested too deep") from e
    return body


def _ends(rows: list[dict]) -> tuple[Any, Any]:
    return (rows[0].get("__id"), rows[-1].get("__id")) if rows else (None, None)


def iter_query(
    session: Any, filters: dict[str, Any], page_size: int = PAGE_SIZE, deadline: float | None = None
) -> Iterator[dict]:
    """Every row matching `filters`, page after page. At the end page 1 is fetched again:
    a different total or first and last row id means the rows moved under the pagination
    (the same total can hide a deletion and an addition), and raises FetchError."""
    total: int | None = None
    seen = 0
    first: tuple[Any, Any] | None = None
    params = {**filters, "__id__sort": "asc", "page_size": page_size}
    for page in range(1, MAX_PAGES + 1):
        body = _page(session, {**params, "page": page}, deadline)
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
            again = _page(session, {**params, "page": 1}, deadline)
            if again["meta"]["total"] != total or _ends(again["data"]) != first:
                raise FetchError("the export changed during the run (page 1 differs at the end)")
            return
    raise FetchError(f"more than {MAX_PAGES} pages ({total} rows)")


def iter_rows(
    session: Any, queries: Iterable[dict[str, Any]], page_size: int = PAGE_SIZE, deadline: float | None = None
) -> Iterator[dict]:
    """Every row of every query, in order. The same row may come from several queries."""
    for filters in queries:
        yield from iter_query(session, filters, page_size, deadline)
