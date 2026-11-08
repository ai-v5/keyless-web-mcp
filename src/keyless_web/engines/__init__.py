"""Search engines plus the multi-engine orchestrator.

``search()`` returns the merged hits; :func:`search_with_report` returns the same
hits plus what each backend did to produce them. Both run the requested engines
in parallel, interleave the hits round-robin so every backend keeps its own
ranking while the merged list stays balanced, de-duplicate by URL, and cache the
result. A single engine failing (block page, timeout, markup change, missing
browser) never fails the whole call -- the remaining engines still answer.

One failure is worse than an engine that returns nothing: an engine that answers
a *different* question. Consumer endpoints do that when they decide the client is
a bot -- Bing replies with a decoy SERP whose hits belong to other queries,
DuckDuckGo with an "anomaly" page -- and such a response is structurally
indistinguishable from a real one. So a group whose hits share no term with the
query is discarded as a whole before the merge, and the report names it.

Only whole groups are discarded, never single hits: a genuinely relevant hit may
share no term with the query at all ("NCCL timeout" -> a fault-tolerance article
that never names the term), and news engines return those routinely. If *every*
non-empty group would be discarded the gate switches itself off for that call,
because turning "some data" into "no data" is the worse failure.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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

_LOG = logging.getLogger("keyless_web.engines")

#: What counts as a term of the query for the relevance gate. A Latin word needs
#: two characters (``ai`` is a term, a stray ``a`` is not); a CJK run stands on
#: its own. Everything else -- quotes, operators, ``+`` -- is punctuation here.
_TERM = re.compile(r"[0-9a-z_]+|[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")

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

#: Why a whole engine answer was thrown away.
NO_HIT_MATCHED = "no hit shared a term with the query"


@dataclass(frozen=True, slots=True)
class EngineHealth:
    """What one backend contributed to a search, and whether it was believed.

    Attributes:
        name: engine name, as accepted by the ``engines`` argument.
        returned: hits the engine produced.
        matched: hits that share at least one term with the query.
        dropped: why the whole answer was discarded, or ``None`` when it was
            kept. A dropped engine contributes no hits to the result.
    """

    name: str
    returned: int
    matched: int
    dropped: str | None = None

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping with the unset ``dropped`` reason omitted."""
        data: dict[str, object] = {
            "name": self.name,
            "returned": self.returned,
            "matched": self.matched,
        }
        if self.dropped is not None:
            data["dropped"] = self.dropped
        return data


@dataclass(frozen=True, slots=True)
class SearchReport:
    """Merged hits plus what each backend did to produce them."""

    results: tuple[SearchResult, ...]
    engines: tuple[EngineHealth, ...]

    def dropped(self) -> tuple[EngineHealth, ...]:
        """The backends whose whole answer was discarded."""
        return tuple(item for item in self.engines if item.dropped is not None)


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
    return list(
        search_with_report(
            query,
            engines=engines,
            max_results=max_results,
            settings=settings,
            transport=transport,
        ).results
    )


def search_with_report(
    query: str,
    *,
    engines: Sequence[str] | None = None,
    max_results: int = 10,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> SearchReport:
    """:func:`search`, plus per-engine health (see :class:`SearchReport`).

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
    if isinstance(cached, SearchReport):
        return cached

    groups = _run_engines(fetchers, text, limit, config, transport)
    kept, health = _gate_by_relevance(text, [name for name, _fetcher in fetchers], groups)
    report = SearchReport(results=tuple(_merge(kept, limit)), engines=tuple(health))

    if report.results and config.search_cache_ttl > 0:
        store.set(key, report, config.search_cache_ttl)
    return report


def _anchor_terms(query: str) -> tuple[str, ...]:
    """The query's terms: what a hit has to share one of to be believed."""
    words = tuple(_TERM.findall(query.lower()))
    long_enough = tuple(word for word in words if len(word) > 1)
    return long_enough or words


def _shares_a_term(terms: Sequence[str], item: SearchResult) -> bool:
    """Whether any term appears in the hit's title, URL or snippet."""
    haystack = f"{item.title}\n{item.url}\n{item.snippet}".lower()
    return any(term in haystack for term in terms)


def _gate_by_relevance(
    query: str,
    names: Sequence[str],
    groups: list[list[SearchResult]],
) -> tuple[list[list[SearchResult]], list[EngineHealth]]:
    """Drop engine answers that clearly answer a different query.

    Only whole groups are judged: one hit that shares nothing with the query is
    ordinary (news engines paraphrase), a whole group that shares nothing is the
    signature of a decoy or block page.
    """
    terms = _anchor_terms(query)
    counts = (
        [sum(1 for item in group if _shares_a_term(terms, item)) for group in groups]
        if terms
        else [len(group) for group in groups]
    )

    non_empty = [index for index, group in enumerate(groups) if group]
    if terms and non_empty and not any(counts[index] for index in non_empty):
        _LOG.warning(
            "search: no engine returned a hit that shares a term with %r; "
            "keeping every answer unfiltered",
            query,
        )
        return groups, [
            EngineHealth(name, len(group), count)
            for name, group, count in zip(names, groups, counts, strict=True)
        ]

    kept: list[list[SearchResult]] = []
    health: list[EngineHealth] = []
    for name, group, count in zip(names, groups, counts, strict=True):
        if group and count == 0:
            _LOG.warning(
                "search: dropped %s, none of its %d hits shares a term with %r",
                name,
                len(group),
                query,
            )
            kept.append([])
            health.append(EngineHealth(name, len(group), count, NO_HIT_MATCHED))
            continue
        kept.append(group)
        health.append(EngineHealth(name, len(group), count))
    return kept, health


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
    "NO_HIT_MATCHED",
    "Engine",
    "EngineHealth",
    "SearchReport",
    "SearchResult",
    "available_engines",
    "clean_snippet",
    "get_engine",
    "search",
    "search_with_report",
    "to_keyless_error",
]
