"""The JSON-lines worker: protocol round-trip, error mapping, concurrency, shutdown.

Every in-process test drives :class:`keyless_web.worker.Worker` over in-memory
pipes, so the suite stays offline (``conftest`` fails loudly on any real socket)
and never spawns an interpreter. The two subprocess tests only exchange a
``ping``; no test here performs a search or a fetch over the network.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any

import pytest

from keyless_web import worker as worker_module
from keyless_web.engines.base import SearchResult
from keyless_web.errors import (
    KEYLESS_BAD_REQUEST,
    KEYLESS_NETWORK,
    KEYLESS_PARSE,
    BadRequestError,
    NetworkError,
    ParseError,
)
from keyless_web.fetch import FetchedDocument
from keyless_web.worker import (
    PROTOCOL_VERSION,
    WORKER_NAME,
    Worker,
    dispatch,
    run_fetch_request,
    run_search_request,
)
from support import REPO_ROOT, SRC_DIR


def _requests(*lines: Any) -> io.StringIO:
    """A request stream from JSON objects (strings pass through verbatim)."""
    body = [line if isinstance(line, str) else json.dumps(line) for line in lines]
    return io.StringIO("".join(f"{line}\n" for line in body))


def _serve(*lines: Any, max_workers: int = 4) -> list[dict[str, Any]]:
    """Run one Worker over the given request lines and return its responses."""
    out = io.StringIO()
    Worker(_requests(*lines), out, max_workers=max_workers).serve()
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _subprocess_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONHOME"}
    env["PYTHONPATH"] = str(SRC_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


@pytest.fixture
def stub_search(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Replace the engine orchestrator with a recorder returning fixed results."""
    seen: dict[str, Any] = {}

    def fake_search(query: str, **kwargs: Any) -> list[SearchResult]:
        seen["query"] = query
        seen.update(kwargs)
        return [
            SearchResult(
                title="Asyncio basics",
                url="https://example.com/asyncio",
                snippet="event loops",
                engine="duckduckgo",
            ),
            SearchResult(
                title="News hit",
                url="https://example.com/news",
                snippet="release",
                engine="google",
                source="Example News",
                date="2026-11-01",
            ),
        ]

    monkeypatch.setattr(worker_module, "run_search", fake_search)
    yield seen


@pytest.fixture
def stub_fetch(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Replace the page fetcher with a recorder returning one document."""
    seen: dict[str, Any] = {}

    def fake_fetch(url: str, **kwargs: Any) -> FetchedDocument:
        seen["url"] = url
        seen.update(kwargs)
        return FetchedDocument(
            url=url,
            status_code=200,
            text="Title: Example\n\nbody text",
            truncated=False,
        )

    monkeypatch.setattr(worker_module, "fetch_document", fake_fetch)
    yield seen


# --------------------------------------------------------------------------- protocol


def test_ping_reports_the_protocol_contract() -> None:
    (response,) = _serve({"id": 1, "method": "ping"})

    assert response["id"] == 1
    assert response["ok"] is True
    result = response["result"]
    assert result["name"] == WORKER_NAME
    assert result["protocol"] == PROTOCOL_VERSION
    assert "duckduckgo" in result["engines"]


def test_ids_are_echoed_verbatim() -> None:
    responses = _serve(
        {"id": 0, "method": "ping"},
        {"id": "abc", "method": "ping"},
        {"id": None, "method": "ping"},
    )

    assert [item["id"] for item in responses] == [0, "abc", None]


def test_blank_lines_are_ignored() -> None:
    responses = _serve("", "   ", {"id": 2, "method": "ping"})

    assert [item["id"] for item in responses] == [2]


def test_malformed_json_is_answered_instead_of_crashing() -> None:
    (response,) = _serve("{not json")

    assert response["id"] is None
    assert response["ok"] is False
    assert response["error"]["code"] == KEYLESS_BAD_REQUEST
    assert "malformed JSON" in response["error"]["message"]


@pytest.mark.parametrize(
    "line",
    [
        '"a string"',
        "[1, 2]",
        '{"id": 1}',
        '{"id": 1, "method": ""}',
        '{"id": 1, "method": 7}',
        '{"id": 1, "method": "ping", "params": []}',
    ],
)
def test_structurally_invalid_requests_are_rejected(line: str) -> None:
    (response,) = _serve(line)

    assert response["ok"] is False
    assert response["error"]["code"] == KEYLESS_BAD_REQUEST


def test_unknown_method_names_the_accepted_ones() -> None:
    (response,) = _serve({"id": 4, "method": "teleport"})

    assert response["ok"] is False
    assert response["error"]["code"] == KEYLESS_BAD_REQUEST
    assert "teleport" in response["error"]["message"]
    assert "search" in response["error"]["hint"]


def test_shutdown_answers_then_stops_reading() -> None:
    responses = _serve(
        {"id": 1, "method": "shutdown"},
        {"id": 2, "method": "ping"},
    )

    assert len(responses) == 1
    assert responses[0]["result"] == {"stopped": True}


def test_missing_params_object_defaults_to_empty() -> None:
    (response,) = _serve({"id": 1, "method": "ping", "params": None})

    assert response["ok"] is True


# --------------------------------------------------------------------------- search


def test_search_maps_engine_results_to_the_result_shape(stub_search: dict[str, Any]) -> None:
    (response,) = _serve({"id": 1, "method": "search", "params": {"query": "asyncio"}})

    assert response["ok"] is True
    result = response["result"]
    assert result["query"] == "asyncio"
    assert result["truncated"] is False  # two hits, limit ten
    assert result["results"][0] == {
        "title": "Asyncio basics",
        "url": "https://example.com/asyncio",
        "snippet": "event loops",
        "engine": "duckduckgo",
    }
    assert result["results"][1]["date"] == "2026-11-01"
    assert result["results"][1]["source"] == "Example News"


def test_search_passes_the_result_limit_and_engines_through(stub_search: dict[str, Any]) -> None:
    _serve(
        {
            "id": 1,
            "method": "search",
            "params": {"query": "asyncio", "maxResults": 3, "engines": ["bing"]},
        }
    )

    assert stub_search["max_results"] == 3
    assert stub_search["engines"] == ["bing"]


def test_search_defaults_and_caps_max_results(stub_search: dict[str, Any]) -> None:
    _serve({"id": 1, "method": "search", "params": {"query": "x"}})
    assert stub_search["max_results"] == 10

    _serve({"id": 2, "method": "search", "params": {"query": "x", "maxResults": 999}})
    assert stub_search["max_results"] == 20

    _serve({"id": 3, "method": "search", "params": {"query": "x", "maxResults": "many"}})
    assert stub_search["max_results"] == 10


def test_truncated_is_false_when_the_limit_is_not_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module, "run_search", lambda *a, **k: [])

    response = _serve({"id": 1, "method": "search", "params": {"query": "nothing"}})[0]

    assert response["result"] == {"query": "nothing", "results": [], "truncated": False}


def test_truncated_is_true_when_the_limit_is_filled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = SearchResult(title="t", url="https://example.com/t", engine="duckduckgo")
    monkeypatch.setattr(worker_module, "run_search", lambda *a, **k: [hit])

    response = _serve({"id": 1, "method": "search", "params": {"query": "x", "maxResults": 1}})[0]

    assert response["result"]["truncated"] is True


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": 5},
        {"query": "x", "engines": "bing"},
        {"query": "x", "engines": [1]},
    ],
)
def test_invalid_search_params_are_bad_requests(params: dict[str, Any]) -> None:
    response = _serve({"id": 1, "method": "search", "params": params})[0]

    assert response["ok"] is False
    assert response["error"]["code"] == KEYLESS_BAD_REQUEST


def test_search_engine_failure_becomes_a_keyless_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise NetworkError("all network backends failed")

    monkeypatch.setattr(worker_module, "run_search", explode)

    (response,) = _serve({"id": 1, "method": "search", "params": {"query": "x"}})

    assert response["ok"] is False
    assert response["error"]["code"] == KEYLESS_NETWORK
    assert response["error"]["hint"]


# --------------------------------------------------------------------------- fetch


def test_fetch_returns_status_text_and_truncation(stub_fetch: dict[str, Any]) -> None:
    (response,) = _serve({"id": 1, "method": "fetch", "params": {"url": "https://example.com/a"}})

    assert response["ok"] is True
    assert response["result"] == {
        "url": "https://example.com/a",
        "statusCode": 200,
        "text": "Title: Example\n\nbody text",
        "truncated": False,
    }
    assert stub_fetch["allow_error_status"] is True


def test_fetch_passes_options_and_clamps_max_chars(stub_fetch: dict[str, Any]) -> None:
    _serve(
        {
            "id": 1,
            "method": "fetch",
            "params": {"url": "https://example.com/a", "maxChars": 5, "includeLinks": True},
        }
    )
    assert stub_fetch["max_chars"] == 500
    assert stub_fetch["include_links"] is True

    _serve({"id": 2, "method": "fetch", "params": {"url": "https://example.com/a"}})
    assert stub_fetch["max_chars"] == 20_000
    assert stub_fetch["include_links"] is False


def test_non_http_url_is_a_bad_url_error() -> None:
    (response,) = _serve(
        {"id": 1, "method": "fetch", "params": {"url": "file:///C:/Windows/win.ini"}}
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "KEYLESS_BAD_URL"


@pytest.mark.parametrize("params", [{}, {"url": ""}, {"url": 3}])
def test_invalid_fetch_params_are_bad_requests(params: dict[str, Any]) -> None:
    response = _serve({"id": 1, "method": "fetch", "params": params})[0]

    assert response["ok"] is False
    assert response["error"]["code"] == KEYLESS_BAD_REQUEST


def test_parse_failure_is_reported_as_a_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise ParseError("could not extract text")

    monkeypatch.setattr(worker_module, "fetch_document", explode)

    (response,) = _serve({"id": 1, "method": "fetch", "params": {"url": "https://e.com/a"}})

    assert response["ok"] is False
    assert response["error"]["code"] == KEYLESS_PARSE


def test_unexpected_failure_is_mapped_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(worker_module, "fetch_document", explode)

    (response,) = _serve({"id": 1, "method": "fetch", "params": {"url": "https://e.com/a"}})

    assert response["ok"] is False
    assert "RuntimeError: boom" in response["error"]["message"]


# --------------------------------------------------------------------------- concurrency


def test_a_slow_request_does_not_block_a_fast_one(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow_search(query: str, **_kwargs: Any) -> list[SearchResult]:
        time.sleep(0.3)
        return [SearchResult(title=query, url="https://example.com/slow", engine="duckduckgo")]

    monkeypatch.setattr(worker_module, "run_search", slow_search)
    monkeypatch.setattr(
        worker_module,
        "fetch_document",
        lambda url, **_kwargs: FetchedDocument(url, 200, "fast body", False),
    )

    out = io.StringIO()
    requests = _requests(
        {"id": "slow", "method": "search", "params": {"query": "q"}},
        {"id": "fast", "method": "fetch", "params": {"url": "https://example.com/f"}},
    )
    Worker(requests, out, max_workers=2).serve()

    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert {item["id"] for item in responses} == {"slow", "fast"}
    # Answers are written as they complete, so the fast fetch overtakes the search.
    assert responses[0]["id"] == "fast"


# --------------------------------------------------------------------------- dispatch


def test_dispatch_is_usable_without_a_stream(stub_search: dict[str, Any]) -> None:
    result = dispatch("search", {"query": "direct"})

    assert result["query"] == "direct"
    assert stub_search["query"] == "direct"


def test_run_search_request_surfaces_an_engine_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unknown(*_args: Any, **_kwargs: Any) -> None:
        raise BadRequestError("unknown engine 'nope'")

    monkeypatch.setattr(worker_module, "run_search", unknown)

    with pytest.raises(BadRequestError):
        run_search_request({"query": "x", "engines": ["nope"]})


def test_run_fetch_request_rejects_a_missing_url() -> None:
    with pytest.raises(BadRequestError):
        run_fetch_request({})


# --------------------------------------------------------------------------- process


def test_module_entry_point_documents_its_protocol() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "keyless_web.worker", "--help"],
        cwd=str(REPO_ROOT),
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    output = (completed.stdout + completed.stderr).lower()
    assert "usage" in output
    assert "keyless-web-worker" in output


def test_a_real_subprocess_answers_ping_over_stdin() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "keyless_web.worker"],
        cwd=str(REPO_ROOT),
        env=_subprocess_env(),
        input=json.dumps({"id": 1, "method": "ping"}) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout.strip())
    assert response["ok"] is True
    assert response["result"]["protocol"] == PROTOCOL_VERSION


def test_subprocess_reports_a_bad_url_without_a_traceback() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "keyless_web.worker"],
        cwd=str(REPO_ROOT),
        env=_subprocess_env(),
        input=json.dumps({"id": 1, "method": "fetch", "params": {"url": "ftp://example.com/x"}})
        + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout.strip())
    assert response["ok"] is False
    assert response["error"]["code"] == "KEYLESS_BAD_URL"
    assert "Traceback" not in completed.stderr


def test_unicode_survives_a_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    def chinese_search(query: str, **_kwargs: Any) -> list[SearchResult]:
        return [SearchResult(title="分布式缓存", url="https://example.com/zh", engine="duckduckgo")]

    monkeypatch.setattr(worker_module, "run_search", chinese_search)

    (response,) = _serve({"id": 1, "method": "search", "params": {"query": "缓存"}})

    assert response["result"]["results"][0]["title"] == "分布式缓存"


def test_ensure_ascii_escapes_non_ascii_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def chinese_search(query: str, **_kwargs: Any) -> list[SearchResult]:
        return [SearchResult(title="分布式缓存", url="https://example.com/zh", engine="duckduckgo")]

    monkeypatch.setattr(worker_module, "run_search", chinese_search)
    out = io.StringIO()

    Worker(
        _requests({"id": 1, "method": "search", "params": {"query": "缓存"}}),
        out,
        ensure_ascii=True,
    ).serve()

    line = out.getvalue().strip()
    assert "\\u" in line
    assert json.loads(line)["result"]["results"][0]["title"] == "分布式缓存"
