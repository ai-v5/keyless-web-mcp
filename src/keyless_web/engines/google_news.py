"""Google News RSS (the ``google`` engine).

``google.com/search`` no longer serves parseable HTML to non-browser clients --
it answers with a JavaScript redirect shell. The News RSS endpoint is plain XML,
needs no key and no JavaScript, so the ``google`` engine means *Google News*:
news and tech coverage rather than the general web index. Results therefore
carry ``source`` (outlet name) and ``date`` (publication date).

The item URL is Google's own article link (``news.google.com/rss/articles/...``).
It is unique per story and opens the article in a browser, but it is a
JavaScript shell rather than the article text, so ``web_fetch`` on it returns
little useful content; fetch the publisher page instead.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from ..errors import ParseError
from .base import SearchResult, clean_snippet

NAME = "google"
ENDPOINT = "https://news.google.com/rss/search"


def search(
    client: httpx.Client,
    query: str,
    max_results: int = 10,
    *,
    lang: str = "en-US",
    country: str = "US",
) -> list[SearchResult]:
    """Search Google News RSS and return at most ``max_results`` hits."""
    response = client.get(
        ENDPOINT,
        params={"q": query, "hl": lang, "gl": country, "ceid": f"{country}:en"},
    )
    response.raise_for_status()
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as exc:
        raise ParseError(
            "google news returned a payload that is not valid RSS",
            cause=exc,
        ) from exc
    results: list[SearchResult] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source_el = item.find("source")
        source_url = (source_el.get("url") or "").strip() if source_el is not None else ""
        source_name = (
            (source_el.text or "").strip() if source_el is not None and source_el.text else ""
        )
        published = (item.findtext("pubDate") or "").strip()
        # ``<link>`` identifies this item and is unique per hit; the ``<source>``
        # URL is the publisher's home page, so using it here would collapse every
        # story from one outlet on the URL de-duplication step.
        results.append(
            SearchResult(
                title=title,
                url=link or source_url,
                snippet=clean_snippet(item.findtext("description") or ""),
                engine=NAME,
                source=source_name or None,
                date=published or None,
            )
        )
        if len(results) >= max_results:
            break
    return results


__all__ = ["ENDPOINT", "NAME", "search"]
