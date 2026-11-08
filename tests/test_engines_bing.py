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
    assert request.url.params["mkt"] == "en-US"


def test_a_cjk_query_asks_for_the_chinese_market() -> None:
    """Without a market Bing answers Chinese queries with unrelated English hits."""
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        bing.search(client, "华为 昇腾 CANN")

    params = seen[0].url.params
    assert params["mkt"] == "zh-CN"
    assert params["setlang"] == "zh-Hans"


def test_max_results_truncates() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = bing.search(client, "python", max_results=1)

    assert len(results) == 1


def test_page_without_results_returns_empty_list() -> None:
    with _client([], "<html><body><p>no results</p></body></html>") as client:
        assert bing.search(client, "zzzqqq") == []


# --------------------------------------------------------------------------- URL unwrapping

_TRACKED = (
    "https://www.bing.com/ck/a?!&amp;&amp;p=0f003769365374a5&amp;ptn=3&amp;ver=2"
    "&amp;u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS9hbHBoYQ&amp;ntb=1"
)


def _page(href: str, title: str = "Alpha") -> str:
    return (
        '<ol id="b_results"><li class="b_algo">'
        f'<h2><a href="{href}">{title}</a></h2>'
        '<div class="b_caption"><p>snippet</p></div>'
        "</li></ol>"
    )


def test_click_tracker_links_are_unwrapped() -> None:
    """Bing wraps organic links in /ck/a?...&u=a1<base64url>; cite the real URL."""
    with _client([], _page(_TRACKED)) as client:
        results = bing.search(client, "python")

    assert results[0].url == "https://example.com/alpha"


def test_click_tracker_without_a_payload_is_left_alone() -> None:
    tracked = "https://www.bing.com/ck/a?!&amp;&amp;p=1&amp;ptn=3"
    with _client([], _page(tracked)) as client:
        results = bing.search(client, "python")

    assert results[0].url == tracked.replace("&amp;", "&")


def test_highlight_markup_does_not_insert_spaces() -> None:
    """Bing wraps query terms in <strong>; joining with spaces would mangle them."""
    html = _page(
        "https://example.com/alpha",
        "GitHub - <strong>deepseek</strong>-ai/deepseek-harness: <strong>DeepSeek</strong> Harness",
    )
    with _client([], html) as client:
        results = bing.search(client, "deepseek harness")

    assert results[0].title == ("GitHub - deepseek-ai/deepseek-harness: DeepSeek Harness")
