"""End-to-end MCP surface, driven through the SDK's in-memory transport.

This is the only test module that imports the MCP SDK: it connects a real
``ClientSession`` to ``keyless_web.server.app`` over in-memory streams, so tool
discovery, argument validation and JSON shaping are exercised as a client sees
them. No sockets, no subprocesses, no network.

Engine and fetch functions are monkeypatched at their import sites inside
``keyless_web.server`` so the round-trip is deterministic and offline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from mcp.client.session import ClientSession
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams

from keyless_web import server as server_mod
from keyless_web.config import (
    ENGINE_NAMES,
    MAX_MAX_CHARS,
    MAX_SEARCH_RESULTS,
    MIN_MAX_CHARS,
)
from keyless_web.engines.base import SearchResult
from keyless_web.errors import KEYLESS_NETWORK, BadRequestError, NetworkError
from support import run_async

SEARCH_TOOL = "web_search"
FETCH_TOOL = "web_fetch"


# --------------------------------------------------------------------------- plumbing


@contextlib.asynccontextmanager
async def _connected() -> AsyncIterator[ClientSession]:
    app = server_mod.app
    assert isinstance(app, MCPServer), "keyless_web.server.app must be an mcp MCPServer"
    lowlevel = app._lowlevel_server
    options = lowlevel.create_initialization_options()
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        server_task = asyncio.create_task(
            lowlevel.run(server_streams[0], server_streams[1], options)
        )
        try:
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                yield session
        finally:
            server_task.cancel()
            with contextlib.suppress(BaseException):
                await server_task


def _tools() -> dict[str, Any]:
    async def main() -> dict[str, Any]:
        async with _connected() as session:
            listed = await session.list_tools()
            return {tool.name: tool for tool in listed.tools}

    return run_async(main())


def _call(name: str, arguments: dict[str, Any]) -> Any:
    async def main() -> Any:
        async with _connected() as session:
            return await session.call_tool(name, arguments)

    return run_async(main())


def _text(result: Any) -> str:
    return "".join(block.text for block in result.content if getattr(block, "text", None))


def _schema(name: str) -> dict[str, Any]:
    return _tools()[name].input_schema


def _array_items(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve ``items`` of a schema that may be ``array | null`` (anyOf)."""
    if "items" in schema:
        return schema["items"]
    for branch in schema.get("anyOf", []):
        if branch.get("type") == "array":
            return branch["items"]
    raise AssertionError(f"schema does not describe an array: {schema}")


def _fake_search(results: list[SearchResult]):
    calls: list[dict[str, Any]] = []

    def search(query: str, *, engines=None, max_results=10, settings=None, transport=None):
        calls.append({"query": query, "engines": engines, "max_results": max_results})
        return list(results)

    search.calls = calls  # type: ignore[attr-defined]
    return search


# --------------------------------------------------------------------------- discovery


def test_server_identifies_itself() -> None:
    async def main() -> Any:
        async with _connected() as session:
            return await session.list_tools()

    tools = run_async(main())
    assert {tool.name for tool in tools.tools} == {SEARCH_TOOL, FETCH_TOOL}


def test_server_name_and_version() -> None:
    async def main() -> Any:
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            lowlevel = server_mod.app._lowlevel_server
            options = lowlevel.create_initialization_options()
            task = asyncio.create_task(lowlevel.run(server_streams[0], server_streams[1], options))
            try:
                async with ClientSession(*client_streams) as session:
                    return await session.initialize()
            finally:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    info = run_async(main())

    assert info.server_info.name == "keyless-web-mcp"
    assert isinstance(info.server_info.version, str) and info.server_info.version


def test_tool_descriptions_exist() -> None:
    tools = _tools()

    assert "keyless" in (tools[SEARCH_TOOL].description or "").lower()
    assert "json" in (tools[SEARCH_TOOL].description or "").lower()
    assert "http" in (tools[FETCH_TOOL].description or "").lower()


# --------------------------------------------------------------------------- web_search schema


def test_web_search_requires_a_query_string() -> None:
    schema = _schema(SEARCH_TOOL)

    assert schema["required"] == ["query"]
    assert schema["properties"]["query"]["type"] == "string"


def test_web_search_max_results_has_bounds() -> None:
    schema = _schema(SEARCH_TOOL)["properties"]["max_results"]

    assert schema["type"] == "integer"
    assert schema["minimum"] == 1
    assert schema["maximum"] == MAX_SEARCH_RESULTS
    assert schema["default"] == 10


def test_web_search_engines_enum_matches_the_supported_engines() -> None:
    items = _array_items(_schema(SEARCH_TOOL)["properties"]["engines"])

    assert items["type"] == "string"
    assert items["enum"] == list(ENGINE_NAMES)


def test_web_search_engines_is_optional() -> None:
    schema = _schema(SEARCH_TOOL)

    assert "engines" not in schema["required"]
    assert schema["properties"]["engines"]["default"] is None


# --------------------------------------------------------------------------- web_fetch schema


def test_web_fetch_requires_a_url_string() -> None:
    schema = _schema(FETCH_TOOL)

    assert schema["required"] == ["url"]
    assert schema["properties"]["url"]["type"] == "string"


def test_web_fetch_max_chars_has_bounds() -> None:
    schema = _schema(FETCH_TOOL)["properties"]["max_chars"]

    assert schema["type"] == "integer"
    assert schema["minimum"] == MIN_MAX_CHARS
    assert schema["maximum"] == MAX_MAX_CHARS
    assert schema["default"] == 20_000


def test_web_fetch_include_links_is_a_boolean_defaulting_to_false() -> None:
    schema = _schema(FETCH_TOOL)["properties"]["include_links"]

    assert schema["type"] == "boolean"
    assert schema["default"] is False


# --------------------------------------------------------------------------- web_search results


def test_web_search_returns_a_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_search(
        [
            SearchResult(
                title="Alpha",
                url="https://example.com/alpha",
                snippet="first hit",
                engine="duckduckgo",
            ),
            SearchResult(title="Beta", url="https://example.com/beta", engine="google"),
        ]
    )
    monkeypatch.setattr(server_mod, "run_search", fake)

    result = _call(SEARCH_TOOL, {"query": "asyncio", "max_results": 3, "engines": ["duckduckgo"]})

    assert result.is_error is False
    payload = json.loads(_text(result))
    assert payload == [
        {
            "title": "Alpha",
            "url": "https://example.com/alpha",
            "snippet": "first hit",
            "engine": "duckduckgo",
        },
        {"title": "Beta", "url": "https://example.com/beta", "snippet": "", "engine": "google"},
    ]
    assert fake.calls == [  # type: ignore[attr-defined]
        {"query": "asyncio", "engines": ["duckduckgo"], "max_results": 3}
    ]


def test_web_search_without_results_returns_an_empty_json_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_mod, "run_search", _fake_search([]))

    text = _text(_call(SEARCH_TOOL, {"query": "zzzqqq"}))

    assert json.loads(text) == []


def test_web_search_reports_a_keyless_error_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*args: Any, **kwargs: Any) -> list[SearchResult]:
        raise BadRequestError("query must not be empty")

    monkeypatch.setattr(server_mod, "run_search", failing)

    result = _call(SEARCH_TOOL, {"query": "x"})

    assert result.is_error is False  # a model can act on text, not on a traceback
    assert _text(result).startswith("error[KEYLESS_BAD_REQUEST]:")


def test_web_search_rejects_an_empty_query() -> None:
    result = _call(SEARCH_TOOL, {"query": "   "})

    assert result.is_error is False
    assert _text(result).startswith("error[KEYLESS_BAD_REQUEST]:")


def test_web_search_rejects_out_of_range_arguments() -> None:
    """The declared bounds are enforced, not just documented."""
    assert (
        _call(SEARCH_TOOL, {"query": "x", "max_results": MAX_SEARCH_RESULTS + 1}).is_error is True
    )
    assert _call(SEARCH_TOOL, {"query": "x", "max_results": 0}).is_error is True


def test_web_search_rejects_an_unknown_engine() -> None:
    result = _call(SEARCH_TOOL, {"query": "x", "engines": ["nosuchengine"]})

    assert result.is_error is True


# --------------------------------------------------------------------------- web_fetch results


def test_web_fetch_returns_the_extracted_text(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_fetch(url: str, *, max_chars: int = 20_000, include_links: bool = False) -> str:
        calls.append({"url": url, "max_chars": max_chars, "include_links": include_links})
        return f"Title: Example\nURL: {url}\n\nbody"

    monkeypatch.setattr(server_mod, "fetch_page", fake_fetch)

    result = _call(
        FETCH_TOOL,
        {"url": "https://example.com/page", "max_chars": 5_000, "include_links": True},
    )

    assert result.is_error is False
    assert _text(result).startswith("Title: Example")
    assert calls == [{"url": "https://example.com/page", "max_chars": 5_000, "include_links": True}]


def test_web_fetch_defaults_come_from_the_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_fetch(url: str, *, max_chars: int = 0, include_links: bool = True) -> str:
        calls.append({"url": url, "max_chars": max_chars, "include_links": include_links})
        return "ok"

    monkeypatch.setattr(server_mod, "fetch_page", fake_fetch)

    _call(FETCH_TOOL, {"url": "https://example.com/page"})

    assert calls == [
        {"url": "https://example.com/page", "max_chars": 20_000, "include_links": False}
    ]


def test_web_fetch_rejects_non_http_urls_without_network() -> None:
    result = _call(FETCH_TOOL, {"url": "ftp://example.com/file.txt"})

    assert result.is_error is False
    assert _text(result).startswith("error[KEYLESS_BAD_URL]:")


def test_web_fetch_reports_network_failures_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*args: Any, **kwargs: Any) -> str:
        raise NetworkError("all network backends failed")

    monkeypatch.setattr(server_mod, "fetch_page", failing)

    result = _call(FETCH_TOOL, {"url": "https://example.com/page"})

    assert result.is_error is False
    assert _text(result).startswith(f"error[{KEYLESS_NETWORK}]:")


def test_web_fetch_rejects_out_of_range_max_chars() -> None:
    result = _call(FETCH_TOOL, {"url": "https://example.com/page", "max_chars": 10})

    assert result.is_error is True
