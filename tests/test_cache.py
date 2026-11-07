"""TTL cache, cache keys and the per-host rate limiter (both clock-injected)."""

from __future__ import annotations

import httpx
import pytest

from keyless_web.cache import (
    TTLCache,
    cache_key,
    get_fetch_cache,
    get_search_cache,
    reset_caches,
)
from keyless_web.config import (
    DEFAULT_FETCH_CACHE_TTL,
    DEFAULT_SEARCH_CACHE_TTL,
    load_settings,
)
from keyless_web.net.transport import HostRateLimiter, default_limiter, reset_limiter
from support import FakeClock, RecordingClock

# --------------------------------------------------------------------------- keys


def test_cache_key_is_stable() -> None:
    assert cache_key("search", "python", 10) == cache_key("search", "python", 10)


@pytest.mark.parametrize(
    "other",
    [
        ("fetch", "python", 10),
        ("search", "python 3", 10),
        ("search", "python", 11),
        ("search", "Python", 10),
        (("search", "python"), 10),
        ("10", "python", "search"),
    ],
)
def test_cache_key_separates_engines_and_arguments(other: tuple) -> None:
    assert cache_key("search", "python", 10) != cache_key(*other)


def test_cache_key_separates_engine_lists() -> None:
    assert cache_key("search", ("duckduckgo",), "q") != cache_key("search", ("google",), "q")


def test_cache_key_is_a_hex_digest() -> None:
    key = cache_key("search", "python")

    assert isinstance(key, str)
    assert len(key) == 64
    assert all(char in "0123456789abcdef" for char in key)


# --------------------------------------------------------------------------- TTL cache


def test_set_then_get_returns_the_value() -> None:
    clock = FakeClock()
    store: TTLCache = TTLCache(10.0, clock=clock.now)

    store.set("k", {"answer": 42})

    assert store.get("k") == {"answer": 42}
    assert "k" in store
    assert len(store) == 1


def test_entry_expires_after_its_ttl() -> None:
    clock = FakeClock()
    store: TTLCache = TTLCache(10.0, clock=clock.now)
    store.set("k", "value")

    clock.advance(9.9)
    assert store.get("k") == "value"

    clock.advance(0.2)
    assert store.get("k") is None
    assert "k" not in store
    assert len(store) == 0


def test_entry_expires_at_the_exact_boundary() -> None:
    clock = FakeClock()
    store: TTLCache = TTLCache(5.0, clock=clock.now)
    store.set("k", "value")

    clock.advance(5.0)

    assert store.get("k") is None


def test_per_entry_ttl_overrides_the_default() -> None:
    clock = FakeClock()
    store: TTLCache = TTLCache(600.0, clock=clock.now)

    store.set("short", "value", ttl=1.0)
    store.set("long", "value")

    clock.advance(2.0)

    assert store.get("short") is None
    assert store.get("long") == "value"


def test_ttl_zero_disables_caching() -> None:
    clock = FakeClock()
    store: TTLCache = TTLCache(600.0, clock=clock.now)

    store.set("k", "value", ttl=0)

    assert store.get("k") is None
    assert len(store) == 0


def test_delete_and_clear() -> None:
    store: TTLCache = TTLCache(600.0, clock=FakeClock().now)
    store.set("a", 1)
    store.set("b", 2)

    store.delete("a")
    assert store.get("a") is None
    assert len(store) == 1

    store.clear()
    assert len(store) == 0
    assert store.get("b") is None


def test_lru_eviction_at_max_entries() -> None:
    store: TTLCache = TTLCache(600.0, max_entries=2, clock=FakeClock().now)
    store.set("a", 1)
    store.set("b", 2)
    assert store.get("a") == 1  # touch "a" so "b" is the least recently used

    store.set("c", 3)

    assert store.get("a") == 1
    assert store.get("c") == 3
    assert store.get("b") is None


def test_hits_and_misses_are_counted() -> None:
    store: TTLCache = TTLCache(600.0, clock=FakeClock().now)
    store.set("k", "value")

    store.get("k")
    store.get("missing")

    assert store.hits == 1
    assert store.misses == 1


# --------------------------------------------------------------------------- singletons


def test_search_and_fetch_caches_are_distinct_singletons() -> None:
    search_cache = get_search_cache()
    fetch_cache = get_fetch_cache()

    assert search_cache is get_search_cache()
    assert fetch_cache is get_fetch_cache()
    assert search_cache is not fetch_cache
    assert search_cache.default_ttl == DEFAULT_SEARCH_CACHE_TTL
    assert fetch_cache.default_ttl == DEFAULT_FETCH_CACHE_TTL


def test_reset_caches_clears_both() -> None:
    get_search_cache().set("s", 1)
    get_fetch_cache().set("f", 2)

    reset_caches()

    assert get_search_cache().get("s") is None
    assert get_fetch_cache().get("f") is None


# --------------------------------------------------------------------------- rate limiter


def test_first_request_to_a_host_does_not_sleep() -> None:
    clock = RecordingClock()
    limiter = HostRateLimiter(1.0, clock=clock.now, sleep=clock.sleep)

    assert limiter.wait("example.com") == 0.0
    assert clock.sleeps == []


def test_second_request_to_the_same_host_waits_the_minimum_interval() -> None:
    clock = RecordingClock()
    limiter = HostRateLimiter(1.0, clock=clock.now, sleep=clock.sleep)
    limiter.wait("example.com")

    slept = limiter.wait("example.com")

    assert slept == pytest.approx(1.0)
    assert clock.sleeps == [pytest.approx(1.0)]


def test_waits_only_the_remaining_time() -> None:
    clock = RecordingClock()
    limiter = HostRateLimiter(1.0, clock=clock.now, sleep=clock.sleep)
    limiter.wait("example.com")
    clock.advance(0.4)

    assert limiter.wait("example.com") == pytest.approx(0.6)


def test_no_wait_once_the_interval_has_passed() -> None:
    clock = RecordingClock()
    limiter = HostRateLimiter(1.0, clock=clock.now, sleep=clock.sleep)
    limiter.wait("example.com")
    clock.advance(5.0)

    assert limiter.wait("example.com") == 0.0
    assert clock.sleeps == []


def test_hosts_are_tracked_independently() -> None:
    clock = RecordingClock()
    limiter = HostRateLimiter(1.0, clock=clock.now, sleep=clock.sleep)
    limiter.wait("example.com")

    assert limiter.wait("example.org") == 0.0
    assert clock.sleeps == []


def test_zero_interval_disables_the_limiter() -> None:
    clock = RecordingClock()
    limiter = HostRateLimiter(0, clock=clock.now, sleep=clock.sleep)

    assert limiter.wait("example.com") == 0.0
    assert limiter.wait("example.com") == 0.0
    assert limiter.min_interval == 0.0
    assert clock.sleeps == []


def test_empty_host_is_not_tracked() -> None:
    clock = RecordingClock()
    limiter = HostRateLimiter(1.0, clock=clock.now, sleep=clock.sleep)

    assert limiter.wait("") == 0.0
    assert clock.sleeps == []


def test_reset_forgets_previous_requests() -> None:
    clock = RecordingClock()
    limiter = HostRateLimiter(1.0, clock=clock.now, sleep=clock.sleep)
    limiter.wait("example.com")

    limiter.reset()

    assert limiter.wait("example.com") == 0.0
    assert clock.sleeps == []


def test_limiter_works_as_an_httpx_request_hook() -> None:
    clock = RecordingClock()
    limiter = HostRateLimiter(2.0, clock=clock.now, sleep=clock.sleep)
    request = httpx.Request("GET", "https://example.com/page")

    limiter(request)
    limiter.hook(request)

    assert clock.sleeps == [pytest.approx(2.0)]


def test_default_limiter_is_shared_and_resettable() -> None:
    first = default_limiter(0.5)
    assert first is default_limiter(0.5)
    assert first.min_interval == 0.5

    reset_limiter()

    assert default_limiter(0.5) is not first


def test_default_limiter_reads_the_interval_it_is_given() -> None:
    limiter = default_limiter(0)

    assert limiter.min_interval == 0.0
    assert load_settings({"KEYLESS_WEB_MIN_INTERVAL": "0"}).min_interval == 0.0
