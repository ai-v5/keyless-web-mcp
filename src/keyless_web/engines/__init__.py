"""Search engines plus the multi-engine orchestrator.

``search()`` is the only entry point callers need. It runs the requested engines
in order, merges results by URL, deduplicates, and caches the merged list. A
single engine failing (block page, timeout, markup change) never fails the whole
call -- the remaining engines still answer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import httpx

from .. import cache as _cache
from ..config import MAX_SEARCH_RESULTS, Settings
from ..config import settings as current_settings
from ..errors import BadRequestError, KeylessWebError
from ..net.transport import AllBackendsFailed, run_with_fallback, to_keyless_error
from . import bing, duckduckgo, google_news, wikipedia
from .base import Engine, SearchResult, clean_snippet

SearchFn = Callable[..., list[SearchResult]]

#: Registry of available engines. ``google`` is Google News RSS (see module doc).
ENGINES: dict[str, SearchFn] = {
    "bing": bing.search,
    "duckduckgo": duckduckgo.search,
    "google": google_news.search,
    "wikipedia": wikipedia.search,
}


def available_engines() -> tuple[str, ...]:
    """Engine names accepted by ``web_search``."""
    return tuple(ENGINES)


def get_engine(name: str) -> SearchFn:
    """Look up an engine by name.

    Raises:
        BadRequestError: the name is not registered.
    """
    try:
        return ENGINES[name]
    except KeyError:
        raise BadRequestError(
            f"unknown engine {name!r}",
            hint=f"Pass one of: {', '.join(available_engines())}.",
        ) from None


def search(
    query: str,
    *,
    engines: Sequence[str] | None = None,
    max_results: int = 10,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[SearchResult]:
    """Search the requested engines in order and merge the deduplicated hits.

    Args:
        query: search terms; must be non-empty.
        engines: engine names in priority order; defaults to the configured list.
        max_results: upper bound on merged hits (clamped to
            :data:`keyless_web.config.MAX_SEARCH_RESULTS`).
        settings: configuration snapshot; defaults to the process settings.
        transport: optional transport override, for tests.

    Returns:
        Merged results, possibly empty when every engine failed or found nothing.

    Raises:
        BadRequestError: empty query or an unknown engine name.
    """
    config = settings or current_settings()
    text = (query or "").strip()
    if not text:
        raise BadRequestError("query must not be empty", hint="Pass at least one search term.")
    limit = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
    names = tuple(engines) if engines else config.engines
    fetchers = [(name, get_engine(name)) for name in names]

    key = _cache.cache_key("search", names, text.lower(), limit)
    store = _cache.get_search_cache()
    cached = store.get(key) if config.search_cache_ttl > 0 else None
    if cached is not None:
        return list(cached)

    merged: list[SearchResult] = []
    seen: set[str] = set()
    for _name, fetcher in fetchers:
        for item in _run_engine(fetcher, text, limit, config, transport):
            url = item.url
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(item)
            if len(merged) >= limit:
                break
        if len(merged) >= limit:
            break

    if merged and config.search_cache_ttl > 0:
        store.set(key, merged, config.search_cache_ttl)
    return merged[:limit]


def _run_engine(
    fetcher: SearchFn,
    query: str,
    limit: int,
    config: Settings,
    transport: httpx.BaseTransport | None,
) -> list[SearchResult]:
    """Run one engine across the network backends; never raises."""
    try:
        return run_with_fallback(
            lambda client: fetcher(client, query, limit),
            settings=config,
            transport=transport,
        )
    except (AllBackendsFailed, httpx.HTTPError, KeylessWebError):
        return []
    except Exception:  # engine returned unusable content; skip it
        return []


__all__ = [
    "ENGINES",
    "Engine",
    "SearchResult",
    "available_engines",
    "clean_snippet",
    "get_engine",
    "search",
    "to_keyless_error",
]
