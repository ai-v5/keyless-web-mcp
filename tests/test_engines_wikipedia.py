"""Wikipedia action API engine, served from a hand-written JSON fixture."""

from __future__ import annotations

from urllib.parse import unquote, urlparse

import httpx
import pytest

from keyless_web.engines import wikipedia
from keyless_web.errors import KEYLESS_PARSE, ParseError
from support import json_responder, load_fixture, make_client

_PAYLOAD = load_fixture("wikipedia_search.json")
_EMPTY = load_fixture("wikipedia_empty.json")


def _client(seen: list[httpx.Request], body: str = _PAYLOAD) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_responder(body)(request)

    return make_client(handler)


def test_engine_name_is_wikipedia() -> None:
    assert wikipedia.NAME == "wikipedia"


def test_parses_search_hits() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = wikipedia.search(client, "python")

    assert len(results) == 2
    assert [result.title for result in results] == [
        "Python (programming language)",
        "Python",
    ]
    assert [result.engine for result in results] == ["wikipedia", "wikipedia"]


def test_page_url_points_at_the_article() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = wikipedia.search(client, "python")

    parsed = urlparse(results[0].url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "en.wikipedia.org"
    assert unquote(parsed.path) == "/wiki/Python_(programming_language)"
    assert results[1].url.endswith("/wiki/Python")


def test_html_markup_is_stripped_from_snippets() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = wikipedia.search(client, "python")

    assert results[0].snippet == "Python is a high-level, general-purpose programming language."
    assert "<span" not in results[0].snippet
    assert "&quot;" not in results[1].snippet
    assert "programming" in results[1].snippet


def test_language_switches_the_host() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = wikipedia.search(client, "python", lang="fr")

    assert seen[0].url.host == "fr.wikipedia.org"
    assert results[0].url.startswith("https://fr.wikipedia.org/wiki/")


def test_request_uses_the_search_api() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        wikipedia.search(client, "asyncio", max_results=7)

    request = seen[0]
    assert request.url.path == "/w/api.php"
    params = request.url.params
    assert params["action"] == "query"
    assert params["list"] == "search"
    assert params["srsearch"] == "asyncio"
    assert params["srlimit"] == "7"
    assert params["format"] == "json"


def test_max_results_truncates() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = wikipedia.search(client, "python", max_results=1)

    assert len(results) == 1


def test_empty_payload_returns_no_results() -> None:
    with _client([], _EMPTY) as client:
        assert wikipedia.search(client, "zzzqqq") == []


def test_payload_without_query_key_returns_no_results() -> None:
    with _client([], '{"batchcomplete": ""}') as client:
        assert wikipedia.search(client, "zzzqqq") == []


def test_api_error_is_a_parse_error() -> None:
    payload = '{"error": {"code": "badvalue", "info": "unrecognized value"}}'
    with _client([], payload) as client, pytest.raises(ParseError) as excinfo:
        wikipedia.search(client, "python")

    assert excinfo.value.code == KEYLESS_PARSE


def test_non_json_payload_raises_parse_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>blocked</html>", request=request)

    with make_client(handler) as client, pytest.raises(ParseError):
        wikipedia.search(client, "python")


def test_non_object_payload_raises_parse_error() -> None:
    with _client([], "[]") as client, pytest.raises(ParseError):
        wikipedia.search(client, "python")


def test_item_without_a_title_is_skipped() -> None:
    payload = (
        '{"query": {"search": [{"snippet": "no title here"},'
        ' {"title": "Python", "snippet": "the language"}]}}'
    )

    with _client([], payload) as client:
        results = wikipedia.search(client, "python")

    assert [result.title for result in results] == ["Python"]


def test_request_identifies_itself_with_the_wikimedia_user_agent() -> None:
    """Wikimedia blocks browser-impersonating agents (HTTP 403)."""
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        wikipedia.search(client, "asyncio")

    user_agent = seen[0].headers["user-agent"]
    assert user_agent == wikipedia.WIKIMEDIA_USER_AGENT
    assert user_agent.startswith("keyless-web-mcp/")
    assert "http" in user_agent
