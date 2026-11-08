"""The relevance gate: a backend that answered a different question is dropped.

The gate has to be strict enough to remove a decoy SERP and loose enough to keep
news hits that never repeat the query's words, so both directions are pinned
here. Stub engines keep the suite offline.
"""

from __future__ import annotations

import httpx
import pytest

from keyless_web.config import load_settings
from keyless_web.engines import (
    ENGINES,
    NO_HIT_MATCHED,
    EngineHealth,
    search,
    search_with_report,
)
from keyless_web.engines.base import SearchResult

OFFLINE = {"KEYLESS_WEB_MIN_INTERVAL": "0"}


def _settings():
    return load_settings(OFFLINE)


def _hit(url: str, title: str, engine: str) -> SearchResult:
    return SearchResult(title=title, url=url, engine=engine)


@pytest.fixture
def stub_engines(monkeypatch: pytest.MonkeyPatch):
    """``install("name")`` registers a stub whose hits live in ``payloads``."""
    payloads: dict[str, list[SearchResult]] = {}

    def install(name: str) -> None:
        def stub(client: httpx.Client, query: str, max_results: int = 10) -> list[SearchResult]:
            return list(payloads[name])[:max_results]

        payloads[name] = []
        monkeypatch.setitem(ENGINES, name, stub)

    return install, payloads


def test_a_group_that_shares_no_term_with_the_query_is_dropped(stub_engines) -> None:
    """The decoy case: 10 plausible hits, none of them about the query."""
    install, payloads = stub_engines
    install("good")
    install("junk")
    payloads["good"] = [_hit("https://example.com/nccl", "NCCL overview", "good")]
    payloads["junk"] = [
        _hit("https://example.com/apartments", "Summer Place Apartment Homes", "junk"),
        _hit("https://example.com/nhl", "Minnesota Wild schedule", "junk"),
    ]

    report = search_with_report("NCCL", engines=["good", "junk"], settings=_settings())

    assert [item.url for item in report.results] == ["https://example.com/nccl"]
    assert report.engines[0] == EngineHealth("good", returned=1, matched=1)
    assert report.engines[1].returned == 2
    assert report.engines[1].matched == 0
    assert report.engines[1].dropped == NO_HIT_MATCHED
    assert [item.name for item in report.dropped()] == ["junk"]


def test_a_group_with_one_matching_hit_is_kept_whole(stub_engines) -> None:
    """News hits paraphrase: one shared term is enough evidence to trust a group."""
    install, payloads = stub_engines
    install("news")
    payloads["news"] = [
        _hit("https://example.com/1", "NCCL watchdog timeout", "news"),
        _hit("https://example.com/2", "Fault tolerant training on EKS", "news"),
    ]

    report = search_with_report("NCCL timeout", engines=["news"], settings=_settings())

    assert len(report.results) == 2
    assert report.engines[0].matched == 1
    assert report.engines[0].dropped is None
    assert report.dropped() == ()


def test_the_gate_switches_off_instead_of_returning_nothing(stub_engines) -> None:
    """Every group unrelated is a broken network, not an empty query: keep the data."""
    install, payloads = stub_engines
    install("alpha")
    install("beta")
    payloads["alpha"] = [_hit("https://example.com/a", "unrelated page", "alpha")]
    payloads["beta"] = [_hit("https://example.com/b", "also unrelated", "beta")]

    report = search_with_report("zzzqqq", engines=["alpha", "beta"], settings=_settings())

    assert len(report.results) == 2
    assert {health.dropped for health in report.engines} == {None}


def test_a_query_without_usable_terms_disables_the_gate(stub_engines) -> None:
    """Operators and punctuation are not evidence; nothing to match against."""
    install, payloads = stub_engines
    install("only")
    payloads["only"] = [_hit("https://example.com/x", "anything at all", "only")]

    report = search_with_report("+++", engines=["only"], settings=_settings())

    assert len(report.results) == 1
    assert report.engines[0].dropped is None


def test_a_cjk_query_matches_on_a_term(stub_engines) -> None:
    install, payloads = stub_engines
    install("zh")
    payloads["zh"] = [
        _hit("https://example.com/zh", "昇腾 910B 训练笔记", "zh"),
        _hit("https://example.com/en", "GPU notes", "en"),
    ]

    report = search_with_report("昇腾 910B 训练", engines=["zh"], settings=_settings())

    assert report.engines[0].matched == 1
    assert report.engines[0].dropped is None


def test_a_single_unrelated_hit_is_dropped_too(stub_engines) -> None:
    install, payloads = stub_engines
    install("good")
    install("noisy")
    payloads["good"] = [_hit("https://example.com/nccl", "NCCL", "good")]
    payloads["noisy"] = [_hit("https://example.com/x", "apartments for rent", "noisy")]

    report = search_with_report("NCCL", engines=["good", "noisy"], settings=_settings())

    assert [item.url for item in report.results] == ["https://example.com/nccl"]
    assert report.engines[1].dropped == NO_HIT_MATCHED


def test_an_empty_engine_is_not_reported_as_dropped(stub_engines) -> None:
    install, payloads = stub_engines
    install("good")
    install("dead")
    payloads["good"] = [_hit("https://example.com/nccl", "NCCL", "good")]

    report = search_with_report("NCCL", engines=["good", "dead"], settings=_settings())

    assert report.engines[1] == EngineHealth("dead", returned=0, matched=0)
    assert report.engines[1].dropped is None


def test_search_still_returns_a_plain_list(stub_engines) -> None:
    install, payloads = stub_engines
    install("good")
    payloads["good"] = [_hit("https://example.com/nccl", "NCCL", "good")]

    results = search("NCCL", engines=["good"], settings=_settings())

    assert isinstance(results, list)
    assert [item.url for item in results] == ["https://example.com/nccl"]


def test_the_report_survives_the_search_cache(stub_engines) -> None:
    install, payloads = stub_engines
    install("good")
    install("junk")
    payloads["good"] = [_hit("https://example.com/nccl", "NCCL", "good")]
    payloads["junk"] = [_hit("https://example.com/junk", "apartments", "junk")]

    first = search_with_report("NCCL", engines=["good", "junk"], settings=_settings())
    payloads["good"] = []  # a second fetch would now answer differently
    second = search_with_report("NCCL", engines=["good", "junk"], settings=_settings())

    assert [item.url for item in second.results] == [item.url for item in first.results]
    assert [item.dropped for item in second.engines] == [item.dropped for item in first.engines]


def test_health_dict_omits_the_reason_for_a_kept_group() -> None:
    kept = EngineHealth("good", returned=2, matched=2).to_dict()
    dropped = EngineHealth("bad", returned=2, matched=0, dropped=NO_HIT_MATCHED).to_dict()

    assert kept == {"name": "good", "returned": 2, "matched": 2}
    assert dropped["dropped"] == NO_HIT_MATCHED


def test_the_gate_never_raises_on_a_malformed_query(stub_engines) -> None:
    """A query made only of punctuation must still search, not explode."""
    install, payloads = stub_engines
    install("only")
    payloads["only"] = [_hit("https://example.com/x", "page", "only")]

    report = search_with_report("???", engines=["only"], settings=_settings())

    assert isinstance(report.results, tuple)
