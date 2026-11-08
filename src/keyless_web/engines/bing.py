"""Bing HTML endpoint (``li.b_algo`` markup).

Bing returns organic results to a plain HTTP client, but the ``href`` of every
title is a click tracker -- ``https://www.bing.com/ck/a?...&u=a1<base64url>`` --
whose ``u`` parameter carries the real target as base64url. Without unwrapping
it, the model would cite ``bing.com/ck/a`` wrappers instead of the page, so
:func:`_unwrap` decodes it here.

``setlang`` is never enough on its own: without a market parameter Bing falls
back to a generic index and answers a Chinese query with unrelated English
results (NBA scores for a query about a Huawei release). :func:`_market_params`
therefore picks the market from the query's script, which is the cheapest signal
that does not need a geo lookup.

The title of each hit is read with ``get_text("")`` rather than a space
separator: Bing wraps query terms in ``<strong>``, and joining children with
spaces would turn ``deepseek-ai`` into ``deepseek -ai``.
"""

from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from .base import SearchResult, clean_snippet

NAME = "bing"
ENDPOINT = "https://www.bing.com/search"

#: CJK ranges plus Hangul and Kana: a query in one of these scripts wants the
#: matching Bing market, not the default English one.
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def search(
    client: httpx.Client,
    query: str,
    max_results: int = 10,
) -> list[SearchResult]:
    """Search Bing and return at most ``max_results`` hits."""
    response = client.get(
        ENDPOINT,
        params={"q": query, "count": str(max_results), "form": "QBLH", **_market_params(query)},
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
                title=_text(anchor),
                url=_unwrap(str(anchor["href"])),
                snippet=clean_snippet(_text(caption) if caption is not None else ""),
                engine=NAME,
            )
        )
        if len(results) >= max_results:
            break
    return results


def _text(node: object) -> str:
    """Text of a soup node with the source spelling preserved.

    Joining child nodes without a separator matters: Bing highlights query terms
    with ``<strong>``, and ``get_text(" ")`` would turn ``deepseek-ai`` into
    ``deepseek -ai``.
    """
    get_text = getattr(node, "get_text", None)
    raw = get_text("", strip=False) if callable(get_text) else str(node)
    return " ".join(str(raw).split())


def _market_params(query: str) -> dict[str, str]:
    """Market and UI language for a query, chosen from its script."""
    if _CJK.search(query):
        return {"mkt": "zh-CN", "setlang": "zh-Hans"}
    return {"mkt": "en-US", "setlang": "en"}


def _unwrap(href: str) -> str:
    """Return the real target of a Bing click-tracker link."""
    if "bing.com/ck/a" not in href and not href.startswith("/ck/a"):
        return href
    encoded = (parse_qs(urlparse(href).query).get("u") or [""])[0]
    if not encoded.startswith("a1"):
        return href
    payload = encoded[2:]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return href
    return decoded if decoded.startswith("http") else href


__all__ = ["ENDPOINT", "NAME", "search"]
