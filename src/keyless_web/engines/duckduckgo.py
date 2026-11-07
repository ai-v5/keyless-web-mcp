"""DuckDuckGo HTML endpoint (``html.duckduckgo.com``).

The no-JavaScript endpoint returns plain HTML, needs no key and no cookie
banner, which makes it the most reliable default engine. Redirect links carry
the real target in the ``uddg`` query parameter.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from .base import SearchResult, clean_snippet

NAME = "duckduckgo"
ENDPOINT = "https://html.duckduckgo.com/html/"


def search(
    client: httpx.Client,
    query: str,
    max_results: int = 10,
) -> list[SearchResult]:
    """Search DuckDuckGo and return at most ``max_results`` hits."""
    response = client.get(ENDPOINT, params={"q": query})
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results: list[SearchResult] = []
    for block in soup.select(".result"):
        anchor = block.select_one(".result__a")
        href = anchor.get("href") if anchor is not None else None
        if anchor is None or not isinstance(href, str) or not href:
            continue
        url = _unwrap(href)
        snippet = block.select_one(".result__snippet")
        results.append(
            SearchResult(
                title=anchor.get_text(" ", strip=True),
                url=url,
                snippet=clean_snippet(snippet.get_text(" ", strip=True) if snippet else ""),
                engine=NAME,
            )
        )
        if len(results) >= max_results:
            break
    return results


def _unwrap(href: str) -> str:
    """Return the real target of a DuckDuckGo redirect link."""
    if "uddg=" in href:
        target = parse_qs(urlparse(href).query).get("uddg", [""])[0]
        if target:
            return target
    return href


__all__ = ["ENDPOINT", "NAME", "search"]
