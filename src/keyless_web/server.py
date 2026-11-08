"""MCP surface: tool declarations and argument validation only.

Business logic lives in :mod:`keyless_web.engines` and :mod:`keyless_web.fetch`,
so this module stays a thin, testable shell. Failures are returned as text
(never raised) because a model can act on an error message but not on a
transport-level traceback.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from . import __version__
from .config import (
    MAX_MAX_CHARS,
    MAX_SEARCH_RESULTS,
    MIN_MAX_CHARS,
)
from .config import (
    settings as current_settings,
)
from .engines import search as run_search
from .errors import KeylessWebError
from .fetch import fetch_page

Engine = Literal["bing", "duckduckgo", "firecrawl", "google", "google_web", "wikipedia"]

INSTRUCTIONS = (
    "Keyless web access: web_search finds pages and news, web_fetch reads a page "
    "as plain text. Quote the source URL next to any claim you took from a tool "
    "result. Both tools need no API key and no configuration."
)

SEARCH_DESCRIPTION = (
    "Search the web without an API key and return a JSON array of results, "
    'each shaped {"title": str, "url": str, "snippet": str, "engine": str} '
    'plus optional "source" and "date" fields for news hits. Every requested '
    "engine runs concurrently and the hits are interleaved round-robin, so one "
    "answer can cite several independent sources; results are de-duplicated by "
    'URL. engines: any of "google_web", "firecrawl", "duckduckgo", "bing", '
    '"google", "wikipedia"; omitted uses the server default (google_web, '
    'duckduckgo, bing, google). "google_web" is the real Google web index '
    "rendered in a headless browser (needs Chrome or Edge installed), "
    '"firecrawl" is a JSON search API that needs no key but is rate limited '
    'per client address, "google" is Google News RSS (news and tech coverage, '
    'no general web index), and "wikipedia" is the '
    "encyclopedia. max_results must be 1-20. Always valid JSON: an empty array [] "
    "means no engine produced results (retry with different terms or another "
    "engine); on failure it returns a text starting with 'error[KEYLESS_...]:' "
    "plus a Hint."
)

FETCH_DESCRIPTION = (
    "Fetch one http(s) URL and return its readable main text for reading or "
    "citation; JSON endpoints are pretty-printed instead. Only http:// and "
    "https:// are accepted. include_links=true also lists up to 30 links found "
    "on the page (useful to discover follow-up URLs). max_chars caps the "
    "returned text and must be 500-100000. On failure it returns a text "
    "starting with 'error[KEYLESS_...]:' plus a Hint instead of raising."
)

app = MCPServer(
    "keyless-web-mcp",
    version=__version__,
    instructions=INSTRUCTIONS,
)

#: Alias kept for callers that used the previous module's name.
mcp = app


def _clamp(value: int | float | str, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


@app.tool(description=SEARCH_DESCRIPTION)
def web_search(
    query: str,
    max_results: Annotated[int, Field(ge=1, le=MAX_SEARCH_RESULTS)] = 10,
    engines: list[Engine] | None = None,
) -> str:
    """Search the web and return a JSON array of {title, url, snippet, engine}."""
    limit = _clamp(max_results, 1, MAX_SEARCH_RESULTS, current_settings().max_results)
    selected = list(engines) if engines else None
    try:
        results = run_search(query, engines=selected, max_results=limit)
    except KeylessWebError as exc:
        return exc.to_text()
    if not results:
        return "[]"
    return json.dumps([item.to_dict() for item in results], ensure_ascii=False, indent=1)


@app.tool(description=FETCH_DESCRIPTION)
def web_fetch(
    url: str,
    max_chars: Annotated[int, Field(ge=MIN_MAX_CHARS, le=MAX_MAX_CHARS)] = 20_000,
    include_links: bool = False,
) -> str:
    """Read a web page or JSON endpoint and return its text content."""
    limit = _clamp(max_chars, MIN_MAX_CHARS, MAX_MAX_CHARS, current_settings().max_chars)
    try:
        return fetch_page(url, max_chars=limit, include_links=bool(include_links))
    except KeylessWebError as exc:
        return exc.to_text()


__all__ = ["INSTRUCTIONS", "app", "mcp", "web_fetch", "web_search"]
