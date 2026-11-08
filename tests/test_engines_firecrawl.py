"""Firecrawl JSON engine, exercised through ``httpx.MockTransport``.

No network: the API response is a hand-written fixture from ``tests/fixtures``.
"""

from __future__ import annotations

import json

import httpx
import pytest

from keyless_web.engines import firecrawl
from keyless_web.errors import ParseError
from support import json_responder, load_fixture, make_client


def _client(seen: list[httpx.Request]) -> httpx.Client:
    body = load_fixture("firecrawl_results.json")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_responder(body)(request)

    return make_client(handler)


def test_engine_name_is_firecrawl() -> None:
    assert firecrawl.NAME == "firecrawl"


def test_posts_the_query_to_the_search_endpoint() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        firecrawl.search(client, "NCCL timeout", max_results=5)

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.scheme == "https"
    assert request.url.host == "api.firecrawl.dev"
    assert request.url.path == "/v2/search"
    assert json.loads(request.content) == {"query": "NCCL timeout", "limit": 5}


def test_parses_the_web_hits() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = firecrawl.search(client, "NCCL")

    assert [result.title for result in results] == [
        "GitHub - NVIDIA/nccl: Optimized primitives for collective multi-GPU communication",
        "NVIDIA Collective Communications Library (NCCL)",
    ]
    assert [result.url for result in results] == [
        "https://github.com/NVIDIA/nccl",
        "https://developer.nvidia.com/nccl",
    ]
    assert [result.engine for result in results] == ["firecrawl", "firecrawl"]
    assert results[0].snippet.startswith("NCCL (pronounced")
    assert results[0].source is None and results[0].date is None


def test_hits_without_a_url_are_skipped() -> None:
    """A hit the model cannot open is noise, not a result."""
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = firecrawl.search(client, "NCCL")

    assert all(result.url for result in results)
    assert all("unusable" not in result.title for result in results)


def test_snippet_whitespace_is_collapsed() -> None:
    """The API keeps page newlines in ``description``; a snippet must be one line."""
    fixture = json.loads(load_fixture("firecrawl_results.json"))
    assert "\n" in fixture["data"]["web"][1]["description"]

    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = firecrawl.search(client, "NCCL")

    assert "\n" not in results[1].snippet
    assert "Topologies are discovered" in results[1].snippet


def test_flat_data_array_is_accepted() -> None:
    """The older response shape puts the hits straight into ``data``."""
    payload = {
        "success": True,
        "data": [{"url": "https://example.com/a", "title": "A", "description": "a page"}],
    }
    with make_client(json_responder(payload)) as client:
        results = firecrawl.search(client, "example")

    assert [result.url for result in results] == ["https://example.com/a"]


def test_long_description_is_truncated() -> None:
    payload = {
        "success": True,
        "data": {
            "web": [{"url": "https://example.com/long", "title": "Long", "description": "x" * 9000}]
        },
    }
    with make_client(json_responder(payload)) as client:
        results = firecrawl.search(client, "long")

    assert len(results[0].snippet) == firecrawl.MAX_SNIPPET


@pytest.mark.parametrize(("max_results", "expected"), [(1, 1), (2, 2), (10, 2)])
def test_max_results_truncates(max_results: int, expected: int) -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = firecrawl.search(client, "NCCL", max_results=max_results)

    assert len(results) == expected


def test_response_without_web_key_returns_no_results() -> None:
    with make_client(json_responder({"success": True, "data": {}})) as client:
        assert firecrawl.search(client, "NCCL") == []


def test_a_reported_failure_raises_a_parse_error() -> None:
    payload = {"success": False, "error": "rate limit exceeded", "code": "RATE_LIMITED"}
    with make_client(json_responder(payload)) as client, pytest.raises(ParseError) as excinfo:
        firecrawl.search(client, "NCCL")

    assert "rate limit exceeded" in excinfo.value.message


def test_a_non_object_payload_raises_a_parse_error() -> None:
    payload = [{"url": "https://example.com"}]
    with make_client(json_responder(payload)) as client, pytest.raises(ParseError):
        firecrawl.search(client, "NCCL")


def test_a_failure_without_a_reason_is_reported_as_such() -> None:
    payload = {"success": False}
    with make_client(json_responder(payload)) as client, pytest.raises(ParseError) as excinfo:
        firecrawl.search(client, "NCCL")

    assert "no reason given" in excinfo.value.message


def test_a_non_json_body_raises_a_parse_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>blocked</html>", request=request)

    with make_client(handler) as client, pytest.raises(ParseError):
        firecrawl.search(client, "NCCL")


def test_http_error_is_not_swallowed() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="slow down", request=request)

    with make_client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        firecrawl.search(client, "NCCL")

    assert calls["n"] == 1, "a rate limit does not clear inside a retry window"


# --------------------------------------------------------------------------- transient failures


def _record_waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(firecrawl, "_sleep", waits.append)
    return waits


def test_empty_body_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The endpoint occasionally answers 200 with no body; that is not "no results"."""
    waits = _record_waits(monkeypatch)
    body = load_fixture("firecrawl_results.json")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text="", request=request)
        return json_responder(body)(request)

    with make_client(handler) as client:
        results = firecrawl.search(client, "NCCL")

    assert calls["n"] == 2
    assert waits == [firecrawl.RETRY_DELAY]
    assert len(results) == 2


def test_server_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    waits = _record_waits(monkeypatch)
    body = load_fixture("firecrawl_results.json")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="upstream unavailable", request=request)
        return json_responder(body)(request)

    with make_client(handler) as client:
        results = firecrawl.search(client, "NCCL")

    assert calls["n"] == 2
    assert waits == [firecrawl.RETRY_DELAY]
    assert len(results) == 2


def test_a_persistent_empty_body_is_given_up_after_the_attempt_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits = _record_waits(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="", request=request)

    with make_client(handler) as client, pytest.raises(ParseError) as excinfo:
        firecrawl.search(client, "NCCL")

    assert calls["n"] == firecrawl.MAX_ATTEMPTS
    assert waits == [firecrawl.RETRY_DELAY, firecrawl.RETRY_DELAY * 2]
    assert "empty body" in excinfo.value.message


def test_a_persistent_server_error_surfaces_as_an_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_waits(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom", request=request)

    with make_client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        firecrawl.search(client, "NCCL")

    assert calls["n"] == firecrawl.MAX_ATTEMPTS
