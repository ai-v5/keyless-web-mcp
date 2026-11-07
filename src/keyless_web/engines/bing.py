"""Bing HTML endpoint -- opt-in only.

Bing requires cookies to return organic results: without them it answers with a
consent/bot page or, from datacenter egress addresses, with spam. It is therefore
not part of the default engine list, but stays available for callers who want it
(``engines=["bing"]``).
"""

from __future__ import annotations

import httpx
from bs4 import BeautifulSoup

from .base import SearchResult, clean_snippet

NAME = "bing"
ENDPOINT = "https://www.bing.com/search"


def search(
    client: httpx.Client,
    query: str,
    max_results: int = 10,
) -> list[SearchResult]:
    """Search Bing and return at most ``max_results`` hits."""
    response = client.get(
        ENDPOINT,
        params={"q": query, "count": str(max_results), "setlang": "en", "form": "QBLH"},
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    results: list[SearchResult] = []
    for block in soup.select("li.b_algo"):
        anchor = block.select_one("h2 a")
        if anchor is None or not anchor.get("href"):
            continue
        caption = block.select_one(".b_caption p") or block.select_one(".b_caption")
        results.append(
            SearchResult(
                title=anchor.get_text(" ", strip=True),
                url=str(anchor["href"]),
                snippet=clean_snippet(caption.get_text(" ", strip=True) if caption else ""),
                engine=NAME,
            )
        )
        if len(results) >= max_results:
            break
    return results


__all__ = ["ENDPOINT", "NAME", "search"]
