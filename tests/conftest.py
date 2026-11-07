"""Shared pytest plumbing.

Rules for the whole suite:
  * offline by default - an autouse guard turns any real socket connection into a
    loud failure, so a missing ``httpx.MockTransport`` can never silently hit the
    internet in CI;
  * ``net`` tests are collected but skipped unless ``--run-net`` is passed;
  * the package is imported from ``src/`` so no install step is needed.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

from support import FIXTURES_DIR, FakeClock, RecordingClock, load_fixture, make_client, run_async

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "", None}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-net",
        action="store_true",
        default=False,
        help="also run tests marked 'net' (they hit the real internet)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "net: talks to the real internet; skipped unless --run-net is given",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-net"):
        return
    skip_net = pytest.mark.skip(reason="network test: rerun with --run-net to enable")
    for item in items:
        if "net" in item.keywords:
            item.add_marker(skip_net)


@pytest.fixture(autouse=True)
def _offline_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Hard-fail on any non-loopback socket connect.

    Loopback stays allowed because the Windows asyncio event loop builds its
    self-pipe out of a connected TCP socket pair.
    """
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection

    def _host_of(address: Any) -> Any:
        return address[0] if isinstance(address, tuple) and address else address

    def _guard(address: Any) -> None:
        if _host_of(address) in _LOCAL_HOSTS:
            return
        raise RuntimeError(
            "offline test-suite tried to open a real network connection to "
            f"{address!r}; inject httpx.MockTransport(handler) through the "
            "'transport=' parameter instead"
        )

    def connect(self: socket.socket, address: Any) -> Any:
        _guard(address)
        return original_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        _guard(address)
        return original_connect_ex(self, address)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        _guard(address)
        return original_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect, raising=True)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex, raising=True)
    monkeypatch.setattr(socket, "create_connection", create_connection, raising=True)
    yield


@pytest.fixture(autouse=True)
def _reset_global_state() -> Iterator[None]:
    """Drop cached settings / caches / limiter so tests cannot leak state."""
    yield
    from keyless_web.cache import reset_caches
    from keyless_web.config import reload_settings
    from keyless_web.net.transport import reset_limiter

    reset_caches()
    reload_settings()
    reset_limiter()


@pytest.fixture
def fixtures_dir() -> Any:
    return FIXTURES_DIR


@pytest.fixture
def fixture_text() -> Any:
    """``fixture_text("ddg_results.html")`` -> file contents as str."""

    def _load(name: str) -> str:
        return load_fixture(name)

    return _load


@pytest.fixture
def client_factory() -> Any:
    """``client_factory(handler)`` -> httpx.Client on a MockTransport, auto-closed."""
    clients = []

    def _make(handler: Any) -> Any:
        client = make_client(handler)
        clients.append(client)
        return client

    yield _make

    for client in clients:
        client.close()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def recording_clock() -> RecordingClock:
    return RecordingClock()


@pytest.fixture
def run() -> Any:
    """``run(coro)`` - drive a coroutine from a synchronous test."""
    return run_async
