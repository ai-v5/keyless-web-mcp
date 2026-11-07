"""Bing HTML engine (opt-in, ``li.b_algo`` markup), fully offline."""

from __future__ import annotations

import httpx

from keyless_web.engines import bing
from support import html_responder, load_fixture, make_client


def _client(seen: list[httpx.Request], body: str | None = None) -> httpx.Client:
    page = load_fixture("bing_results.html") if body is None else body

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return html_responder(page)(request)

    return make_client(handler)


def test_engine_name_is_bing() -> None:
    assert bing.NAME == "bing"


def test_parses_b_algo_blocks() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = bing.search(client, "python")

    assert len(results) == 2
    assert [result.title for result in results] == ["Alpha & Python", "Beta result"]
    assert [result.url for result in results] == [
        "https://example.com/alpha",
        "https://example.org/beta",
    ]
    assert [result.engine for result in results] == ["bing", "bing"]
    assert results[0].snippet == "Alpha snippet from Bing about Python."


def test_sidebar_blocks_are_ignored() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = bing.search(client, "python")

    assert all("Sidebar" not in result.snippet for result in results)
    assert all("Sidebar" not in result.title for result in results)


def test_request_hits_the_bing_search_endpoint() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        bing.search(client, "python asyncio")

    request = seen[0]
    assert request.url.host == "www.bing.com"
    assert request.url.path == "/search"
    assert request.url.params["q"] == "python asyncio"


def test_max_results_truncates() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = bing.search(client, "python", max_results=1)

    assert len(results) == 1


def test_page_without_results_returns_empty_list() -> None:
    with _client([], "<html><body><p>no results</p></body></html>") as client:
        assert bing.search(client, "zzzqqq") == []
