"""Search engines plus the multi-engine orchestrator.

``search()`` is the only entry point callers need. It runs the requested engines
in parallel, then interleaves their hits round-robin so every backend keeps its
own ranking while the merged list stays balanced, de-duplicates by URL, and
caches the result. A single engine failing (block page, timeout, markup change,
missing browser) never fails the whole call -- the remaining engines still
answer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import httpx

from .. import cache as _cache
from ..config import MAX_SEARCH_RESULTS, Settings
from ..config import settings as current_settings
from ..errors import BadRequestError, KeylessWebError
from ..net.transport import AllBackendsFailed, run_with_fallback, to_keyless_error
from . import bing, duckduckgo, firecrawl, google_news, google_web, wikipedia
from .base import Engine, SearchResult, clean_snippet

SearchFn = Callable[..., list[SearchResult]]

#: Registry of available engines. ``google`` is Google News RSS (see module doc)
#: and ``google_web`` is the rendered web index behind a headless browser.
ENGINES: dict[str, SearchFn] = {
    "bing": bing.search,
    "duckduckgo": duckduckgo.search,
    "firecrawl": firecrawl.search,
    "google": google_news.search,
    "google_web": google_web.search,
    "wikipedia": wikipedia.search,
}

#: Cap on engines queried at the same time. More concurrency buys nothing: each
#: engine talks to a different host and the slowest one sets the wall clock.
_MAX_PARALLEL_ENGINES = 6


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
    """Search the requested engines and merge the deduplicated hits.

    Every configured engine runs, so a merged answer can cite more than one
    source; the engines are queried concurrently, which keeps the wall clock at
    the slowest backend instead of the sum of all of them.

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

    groups = _run_engines(fetchers, text, limit, config, transport)
    merged = _merge(groups, limit)

    if merged and config.search_cache_ttl > 0:
        store.set(key, merged, config.search_cache_ttl)
    return merged


def _run_engines(
    fetchers: list[tuple[str, SearchFn]],
    query: str,
    limit: int,
    config: Settings,
    transport: httpx.BaseTransport | None,
) -> list[list[SearchResult]]:
    """Run every engine concurrently and return their hits in configured order."""
    if len(fetchers) == 1:
        _name, fetcher = fetchers[0]
        return [_run_engine(fetcher, query, limit, config, transport)]

    workers = max(1, min(_MAX_PARALLEL_ENGINES, len(fetchers)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_run_engine, fetcher, query, limit, config, transport)
            for _name, fetcher in fetchers
        ]
        return [future.result() for future in futures]


def _merge(groups: list[list[SearchResult]], limit: int) -> list[SearchResult]:
    """Interleave engine hit lists round-robin, de-duplicating by URL.

    Round-robin keeps each engine's own ranking intact (its first hit is still
    its best hit) while guaranteeing that a backend which returns fewer results
    does not disappear behind one that returns many.
    """
    merged: list[SearchResult] = []
    seen: set[str] = set()
    queues = [list(group) for group in groups]
    while len(merged) < limit and any(queues):
        for queue in queues:
            while queue:
                item = queue.pop(0)
                key = _dedupe_key(item.url)
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(item)
                break
            if len(merged) >= limit:
                break
    return merged


def _dedupe_key(url: str) -> str:
    """Comparison key for de-duplication: host case, fragment and trailing ``/``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.netloc:
        return url
    path = parts.path.rstrip("/")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"


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
