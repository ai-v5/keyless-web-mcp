"""Shared result model, snippet cleaning and the multi-engine orchestrator.

The orchestrator is tested with *stub* engines injected into ``ENGINES`` so the
tests stay offline and no real parsing is involved.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from keyless_web import cache as cache_mod
from keyless_web.config import ENGINE_NAMES, MAX_SEARCH_RESULTS, load_settings
from keyless_web.engines import (
    ENGINES,
    available_engines,
    bing,
    duckduckgo,
    get_engine,
    google_news,
    google_web,
    search,
    wikipedia,
)
from keyless_web.engines.base import SearchResult, clean_snippet
from keyless_web.errors import KEYLESS_BAD_REQUEST, BadRequestError
from support import load_fixture

OFFLINE = {"KEYLESS_WEB_MIN_INTERVAL": "0"}


def _settings(**extra: str):
    return load_settings({**OFFLINE, **extra})


def _result(url: str, title: str = "t", engine: str = "stub") -> SearchResult:
    return SearchResult(title=title, url=url, engine=engine)


# --------------------------------------------------------------------------- model


def test_search_result_defaults() -> None:
    item = SearchResult(title="t", url="https://example.com/a", engine="duckduckgo")

    assert item.snippet == ""
    assert item.source is None
    assert item.date is None


def test_search_result_requires_engine() -> None:
    with pytest.raises(ValidationError):
        SearchResult(title="t", url="https://example.com/a")  # type: ignore[call-arg]


def test_to_dict_omits_unset_optional_fields() -> None:
    item = SearchResult(title="t", url="https://example.com/a", snippet="s", engine="bing")

    assert item.to_dict() == {
        "title": "t",
        "url": "https://example.com/a",
        "snippet": "s",
        "engine": "bing",
    }


def test_to_dict_keeps_news_fields() -> None:
    item = SearchResult(
        title="t",
        url="https://example.com/a",
        snippet="s",
        engine="google",
        source="Example Wire",
        date="Mon, 02 Nov 2026 08:15:00 GMT",
    )

    payload = item.to_dict()
    assert payload["source"] == "Example Wire"
    assert payload["date"] == "Mon, 02 Nov 2026 08:15:00 GMT"


# --------------------------------------------------------------------------- cleaning


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("plain text", "plain text"),
        ("<b>Hello</b> &amp; bye", "Hello & bye"),
        ("line one\n\tline two", "line one line two"),
        ("a  b   c", "a b c"),
        ("&quot;quoted&quot;", '"quoted"'),
    ],
)
def test_clean_snippet(raw: str | None, expected: str) -> None:
    assert clean_snippet(raw) == expected


# --------------------------------------------------------------------------- registry


def test_available_engines_matches_the_public_contract() -> None:
    assert available_engines() == ENGINE_NAMES
    assert set(available_engines()) == {
        "bing",
        "duckduckgo",
        "google",
        "google_web",
        "wikipedia",
    }


def test_registry_maps_names_to_the_engine_functions() -> None:
    assert get_engine("duckduckgo") is duckduckgo.search
    assert get_engine("google") is google_news.search
    assert get_engine("google_web") is google_web.search
    assert get_engine("wikipedia") is wikipedia.search
    assert get_engine("bing") is bing.search


def test_unknown_engine_raises_bad_request() -> None:
    with pytest.raises(BadRequestError) as excinfo:
        get_engine("nosuchengine")

    error = excinfo.value
    assert error.code == KEYLESS_BAD_REQUEST
    assert "nosuchengine" in error.message
    assert "duckduckgo" in error.hint


# --------------------------------------------------------------------------- orchestration


@pytest.fixture
def stub_engines(monkeypatch: pytest.MonkeyPatch):
    """Install two stub engines and return a call counter per engine."""
    calls = {"alpha": 0, "beta": 0}
    payloads: dict[str, list[SearchResult]] = {"alpha": [], "beta": []}

    def make(name: str):
        def stub(client: httpx.Client, query: str, max_results: int = 10) -> list[SearchResult]:
            calls[name] += 1
            return list(payloads[name])[:max_results]

        return stub

    monkeypatch.setitem(ENGINES, "alpha", make("alpha"))
    monkeypatch.setitem(ENGINES, "beta", make("beta"))
    return calls, payloads


def test_search_merges_engines_and_dedupes_by_url(stub_engines) -> None:
    calls, payloads = stub_engines
    payloads["alpha"] = [_result("https://example.com/1"), _result("https://example.com/2")]
    payloads["beta"] = [_result("https://example.com/2"), _result("https://example.com/3")]

    results = search("query", engines=["alpha", "beta"], settings=_settings())

    assert [item.url for item in results] == [
        "https://example.com/1",
        "https://example.com/2",
        "https://example.com/3",
    ]
    assert calls == {"alpha": 1, "beta": 1}


def test_search_truncates_to_max_results(stub_engines) -> None:
    _, payloads = stub_engines
    payloads["alpha"] = [_result(f"https://example.com/{index}") for index in range(5)]

    results = search("query", engines=["alpha"], max_results=2, settings=_settings())

    assert len(results) == 2


def test_search_clamps_to_the_hard_cap(stub_engines) -> None:
    _, payloads = stub_engines
    payloads["alpha"] = [_result(f"https://example.com/{index}") for index in range(40)]

    results = search("query", engines=["alpha"], max_results=999, settings=_settings())

    assert len(results) == MAX_SEARCH_RESULTS


def test_failing_engine_does_not_fail_the_call(
    stub_engines, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, payloads = stub_engines
    payloads["beta"] = [_result("https://example.com/3")]

    def broken(client: httpx.Client, query: str, max_results: int = 10):
        raise RuntimeError("markup changed")

    monkeypatch.setitem(ENGINES, "alpha", broken)

    results = search("query", engines=["alpha", "beta"], settings=_settings())

    assert [item.url for item in results] == ["https://example.com/3"]


def test_search_stops_when_every_engine_fails(stub_engines) -> None:
    _, payloads = stub_engines
    payloads["alpha"] = []
    payloads["beta"] = []

    assert search("query", engines=["alpha", "beta"], settings=_settings()) == []


def test_empty_query_is_rejected() -> None:
    with pytest.raises(BadRequestError) as excinfo:
        search("   ", engines=["duckduckgo"], settings=_settings())

    assert excinfo.value.code == KEYLESS_BAD_REQUEST


def test_unknown_engine_in_the_request_is_rejected() -> None:
    with pytest.raises(BadRequestError):
        search("query", engines=["nosuchengine"], settings=_settings())


def test_results_are_cached_between_identical_calls(stub_engines) -> None:
    calls, payloads = stub_engines
    payloads["alpha"] = [_result("https://example.com/1")]

    first = search("query", engines=["alpha"], settings=_settings())
    second = search("query", engines=["alpha"], settings=_settings())

    assert calls["alpha"] == 1
    assert [item.url for item in second] == [item.url for item in first]
    assert second is not first  # a copy, so callers cannot mutate the cache


def test_cache_is_bypassed_when_ttl_is_zero(stub_engines) -> None:
    calls, payloads = stub_engines
    payloads["alpha"] = [_result("https://example.com/1")]

    settings = _settings(KEYLESS_WEB_CACHE_TTL="0")
    search("query", engines=["alpha"], settings=settings)
    search("query", engines=["alpha"], settings=settings)

    assert calls["alpha"] == 2


def test_cache_is_scoped_to_engine_and_query(stub_engines) -> None:
    calls, payloads = stub_engines
    payloads["alpha"] = [_result("https://example.com/1")]
    payloads["beta"] = [_result("https://example.com/2")]

    search("one", engines=["alpha"], settings=_settings())
    search("two", engines=["alpha"], settings=_settings())
    search("one", engines=["beta"], settings=_settings())

    assert calls == {"alpha": 2, "beta": 1}


def test_search_uses_the_configured_engine_list(
    stub_engines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``engines`` is omitted, ``Settings.engines`` decides what runs."""
    calls, payloads = stub_engines
    payloads["alpha"] = [_result("https://example.com/1")]
    monkeypatch.setitem(ENGINES, "wikipedia", ENGINES["alpha"])

    settings = _settings(KEYLESS_WEB_ENGINES="wikipedia")
    search("query", settings=settings)

    assert calls["alpha"] == 1
    assert calls["beta"] == 0
    assert settings.engines == ("wikipedia",)


def test_engines_receive_the_configured_limit(
    stub_engines, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    def stub(client: httpx.Client, query: str, max_results: int = 10) -> list[SearchResult]:
        seen.append(max_results)
        return [_result("https://example.com/1")]

    monkeypatch.setitem(ENGINES, "alpha", stub)

    search("query", engines=["alpha"], max_results=7, settings=_settings())

    assert seen == [7]


def test_fixture_directory_is_used_for_engine_smoke() -> None:
    """Guards against a fixture move silently disabling the offline engine tests."""
    assert "result__a" in load_fixture("ddg_results.html")
    assert "<rss" in load_fixture("google_news.rss")
    assert cache_mod.cache_key("search", "q") != cache_mod.cache_key("fetch", "q")
