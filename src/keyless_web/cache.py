"""In-process TTL cache for search results and fetched pages.

Both tools are frequently re-called inside one agent turn (the model re-reads
the page it just discovered, or asks the same query twice). A short-lived
process cache removes most of that traffic without affecting correctness: the
key includes every parameter that changes the answer, so a cache hit is
indistinguishable from a fresh call except for the latency.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from .config import DEFAULT_FETCH_CACHE_TTL, DEFAULT_SEARCH_CACHE_TTL

T = TypeVar("T")


def cache_key(*parts: object) -> str:
    """Build a stable cache key from arbitrary JSON-serializable parts.

    The engine name, the query and every output-affecting argument must be
    passed as separate parts, so different inputs can never collide.
    """
    payload = json.dumps(parts, sort_keys=True, default=repr, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Entry:
    value: Any
    expires_at: float


class TTLCache:
    """Small thread-safe TTL cache with LRU eviction.

    Args:
        default_ttl: seconds an entry stays valid unless overridden per entry.
        max_entries: upper bound on retained entries.
        clock: monotonic time source, injectable for tests.
    """

    def __init__(
        self,
        default_ttl: float = DEFAULT_SEARCH_CACHE_TTL,
        *,
        max_entries: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.default_ttl = float(default_ttl)
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        """Return the cached value, or ``None`` when missing or expired."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            if entry.expires_at <= self._clock():
                del self._entries[key]
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry.value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """Store ``value``; a TTL of ``0`` (or less) disables caching."""
        seconds = self.default_ttl if ttl is None else float(ttl)
        if seconds <= 0:
            return
        expires_at = self._clock() + seconds
        with self._lock:
            self._entries[key] = _Entry(value=value, expires_at=expires_at)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.hits = 0
            self.misses = 0

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.get(key) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_SEARCH_CACHE: TTLCache | None = None
_FETCH_CACHE: TTLCache | None = None


def get_search_cache() -> TTLCache:
    """Process-wide cache for search results (default TTL 300s)."""
    global _SEARCH_CACHE
    if _SEARCH_CACHE is None:
        _SEARCH_CACHE = TTLCache(DEFAULT_SEARCH_CACHE_TTL)
    return _SEARCH_CACHE


def get_fetch_cache() -> TTLCache:
    """Process-wide cache for fetched page text (default TTL 900s)."""
    global _FETCH_CACHE
    if _FETCH_CACHE is None:
        _FETCH_CACHE = TTLCache(DEFAULT_FETCH_CACHE_TTL)
    return _FETCH_CACHE


def reset_caches() -> None:
    """Clear both process caches (used by tests and by ``--clear-cache``)."""
    get_search_cache().clear()
    get_fetch_cache().clear()


__all__ = ["TTLCache", "cache_key", "get_fetch_cache", "get_search_cache", "reset_caches"]
