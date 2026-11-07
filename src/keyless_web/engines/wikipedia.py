"""Wikipedia search API -- an encyclopedic baseline that never rate-limits hard.

The MediaWiki action API is public, keyless and stable, so this engine is the
most dependable fallback. It answers questions of the "what is X" shape rather
than "which page mentions X", so it complements the HTML scrapers.

Wikimedia's robot policy (https://meta.wikimedia.org/wiki/User-Agent_policy)
requires API clients to identify themselves with a descriptive User-Agent and
**blocks browser-impersonating agents with HTTP 403**, which is why this engine
overrides the shared browser User-Agent for its own request.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from .. import __version__
from ..errors import ParseError
from .base import SearchResult, clean_snippet

NAME = "wikipedia"

#: Descriptive agent required by the Wikimedia API policy.
WIKIMEDIA_USER_AGENT = f"keyless-web-mcp/{__version__} (+https://github.com/ai-v5/keyless-web-mcp)"


def search(
    client: httpx.Client,
    query: str,
    max_results: int = 10,
    *,
    lang: str = "en",
) -> list[SearchResult]:
    """Search a Wikipedia edition and return at most ``max_results`` hits."""
    endpoint = f"https://{lang}.wikipedia.org/w/api.php"
    response = client.get(
        endpoint,
        params={
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": str(max_results),
            "format": "json",
        },
        headers={"User-Agent": WIKIMEDIA_USER_AGENT},
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise ParseError(
            "wikipedia returned a payload that is not JSON",
            cause=exc,
        ) from exc
    if not isinstance(payload, dict):
        raise ParseError("wikipedia returned an unexpected JSON shape")
    if "error" in payload:
        raise ParseError(f"wikipedia API error: {payload['error']}")
    results: list[SearchResult] = []
    for item in payload.get("query", {}).get("search", []):
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        page_url = f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
        results.append(
            SearchResult(
                title=title,
                url=page_url,
                snippet=clean_snippet(item.get("snippet", "")),
                engine=NAME,
            )
        )
        if len(results) >= max_results:
            break
    return results


__all__ = ["NAME", "WIKIMEDIA_USER_AGENT", "search"]
