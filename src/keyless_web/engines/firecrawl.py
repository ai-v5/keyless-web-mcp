"""Firecrawl search API (``api.firecrawl.dev/v2/search``) -- a JSON index, not a SERP.

Every other engine in this package parses a result page built for a browser, and
those pages are free to answer a scripted client however they like: Google hands
back a JavaScript shell, DuckDuckGo answers HTTP 202 with an "anomaly" page, and
Bing answers with a decoy SERP whose results belong to *other* queries. This
engine talks to a JSON endpoint instead, so there is no markup to drift and no
page template to misread.

The endpoint needs no account and no API key. It is rate limited per client
address, so an HTTP 429 is an expected outcome rather than a bug; the transport
layer already maps that onto ``KEYLESS_RATE_LIMITED``, and it is *not* retried
here because the limit does not clear in the seconds a retry would take.

Two failures are transient on the provider's side and are retried: an HTTP 5xx,
and the empty ``200 OK`` this endpoint occasionally returns. Neither is allowed
to look like "no results" -- that would hide a working query behind a broken
response.

Each hit carries a ``description`` that Firecrawl rendered from the page itself,
which is much longer than a consumer SERP snippet: across fifty sampled hits the
median was 162 characters but the tail reached 12.8 KB of page markdown. It is
cut at :data:`MAX_SNIPPET` so one engine cannot crowd out the others; raise that
constant when the caller has context to spare.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..errors import ParseError
from .base import SearchResult, clean_snippet

NAME = "firecrawl"
ENDPOINT = "https://api.firecrawl.dev/v2/search"

#: Upper bound on a hit's snippet, in characters, after whitespace is collapsed.
MAX_SNIPPET = 4000

#: Total attempts for one call when the answer is a 5xx or an empty 200.
MAX_ATTEMPTS = 3

#: Seconds before the second attempt; the third waits twice as long.
RETRY_DELAY = 0.5

#: HTTP status at which an answer counts as the server's fault, not ours.
_SERVER_ERROR = 500

#: Indirection so the offline suite can retry without waiting.
_sleep = time.sleep


def search(
    client: httpx.Client,
    query: str,
    max_results: int = 10,
) -> list[SearchResult]:
    """Search the Firecrawl index and return at most ``max_results`` hits."""
    response = _request(client, query, max(1, min(int(max_results), 20)))
    try:
        payload = response.json()
    except ValueError as exc:
        raise ParseError(
            "firecrawl returned a payload that is not JSON",
            cause=exc,
        ) from exc
    if not isinstance(payload, dict):
        raise ParseError("firecrawl returned an unexpected JSON shape")
    if payload.get("success") is False:
        detail = payload.get("error") or payload.get("code") or "no reason given"
        raise ParseError(f"firecrawl reported a failure: {detail}")

    results: list[SearchResult] = []
    for item in _hits(payload):
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        results.append(
            SearchResult(
                title=_text(item.get("title")),
                url=url,
                snippet=_snippet(item.get("description")),
                engine=NAME,
            )
        )
        if len(results) >= max_results:
            break
    return results


def _request(client: httpx.Client, query: str, limit: int) -> httpx.Response:
    """POST the search, retrying the transient failures described above.

    A 4xx is returned as-is (rate limit, blocked client): retrying cannot help,
    and :meth:`httpx.Response.raise_for_status` turns it into the error the
    transport layer already knows how to report.
    """
    body = {"query": query, "limit": limit}
    response = client.post(ENDPOINT, json=body)
    for attempt in range(1, MAX_ATTEMPTS):
        transient = response.status_code >= _SERVER_ERROR or (
            response.status_code < 300 and not response.content
        )
        if not transient:
            break
        _sleep(RETRY_DELAY * attempt)
        response = client.post(ENDPOINT, json=body)
    if response.status_code < 300 and not response.content:
        raise ParseError(
            f"firecrawl answered with an empty body ({MAX_ATTEMPTS} attempts, "
            f"HTTP {response.status_code})"
        )
    response.raise_for_status()
    return response


def _hits(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The result array, in either documented response shape.

    ``v2`` nests the web hits under ``data.web``; the older shape (and the one
    the endpoint still answers when a caller pins it) puts the array straight in
    ``data``. Both are accepted so a version bump cannot silently empty the
    engine.
    """
    data = payload.get("data")
    hits = data.get("web") if isinstance(data, dict) else data
    if not isinstance(hits, list):
        return []
    return [item for item in hits if isinstance(item, dict)]


def _text(value: object) -> str:
    """Collapse whitespace in a title; the API sometimes keeps page-newlines."""
    return " ".join(str(value or "").split())


def _snippet(value: object) -> str:
    """Clean a description and cut it to :data:`MAX_SNIPPET` characters."""
    return clean_snippet(str(value or ""))[:MAX_SNIPPET]


__all__ = ["ENDPOINT", "MAX_ATTEMPTS", "MAX_SNIPPET", "NAME", "RETRY_DELAY", "search"]
