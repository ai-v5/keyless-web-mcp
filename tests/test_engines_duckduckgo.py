"""DuckDuckGo HTML endpoint parser, exercised through ``httpx.MockTransport``.

No network: every response is a hand-written fixture from ``tests/fixtures``.
"""

from __future__ import annotations

import httpx
import pytest

from keyless_web.engines import duckduckgo
from keyless_web.engines.base import SearchResult
from support import html_responder, load_fixture, make_client


def _client(seen: list[httpx.Request]) -> httpx.Client:
    body = load_fixture("ddg_results.html")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return html_responder(body)(request)

    return make_client(handler)


def test_engine_name_is_duckduckgo() -> None:
    assert duckduckgo.NAME == "duckduckgo"


def test_parses_every_organic_result() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = duckduckgo.search(client, "python")

    assert len(results) == 3
    assert all(isinstance(result, SearchResult) for result in results)
    assert [result.title for result in results] == [
        "Alpha & the Python Language",
        "Beta page with query string",
        "Gamma absolute href",
    ]
    assert [result.url for result in results] == [
        "https://example.com/alpha",
        "https://example.org/beta?x=1&y=2",
        "https://example.net/gamma",
    ]
    assert [result.engine for result in results] == ["duckduckgo"] * 3
    assert results[0].snippet == "Alpha snippet: a short description of the first hit."


def test_uddg_redirect_is_unwrapped() -> None:
    """Redirect links carry the real target percent-encoded in ``uddg=``."""
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = duckduckgo.search(client, "python")

    second = results[1]
    assert second.url == "https://example.org/beta?x=1&y=2"
    assert "uddg" not in second.url
    assert "duckduckgo.com" not in second.url
    assert not any("duckduckgo.com/l/" in result.url for result in results)


def test_request_goes_to_html_endpoint_with_the_query() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        duckduckgo.search(client, "c++ & rust")

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET"
    assert request.url.host == "html.duckduckgo.com"
    assert request.url.params["q"] == "c++ & rust"


def test_empty_page_returns_no_results() -> None:
    body = load_fixture("ddg_empty.html")
    with make_client(html_responder(body)) as client:
        results = duckduckgo.search(client, "zzzqqq")

    assert results == []


def test_unparseable_body_returns_no_results() -> None:
    """A block page or an empty body must not raise a parse error."""
    with make_client(html_responder("")) as client:
        assert duckduckgo.search(client, "python") == []


@pytest.mark.parametrize(("max_results", "expected"), [(1, 1), (2, 2), (10, 3)])
def test_max_results_truncates(max_results: int, expected: int) -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = duckduckgo.search(client, "python", max_results=max_results)

    assert len(results) == expected


def test_result_dict_omits_unset_optional_fields() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = duckduckgo.search(client, "python")

    payload = results[0].to_dict()
    assert set(payload) == {"title", "url", "snippet", "engine"}
    assert "source" not in payload and "date" not in payload


def test_http_error_is_not_swallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom", request=request)

    with make_client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        duckduckgo.search(client, "python")
