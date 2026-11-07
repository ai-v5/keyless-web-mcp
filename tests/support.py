"""Importable helpers shared by the offline test-suite.

Deliberately kept free of pytest fixtures: ``tests/conftest.py`` imports this
module, and individual tests may ``from support import ...`` directly.

Nothing here touches the network: every HTTP call is served by an
``httpx.MockTransport`` so the suite stays runnable in CI without DNS.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Make ``import keyless_web`` work from a plain checkout (src layout, not installed).
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def load_fixture(name: str) -> str:
    """Return the text of ``tests/fixtures/<name>`` (UTF-8)."""
    path = FIXTURES_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"missing test fixture {name!r} (expected at {path})")
    return path.read_text(encoding="utf-8")


def html_responder(body: str, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    """A MockTransport handler that always answers with ``body`` as HTML."""

    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            text=body,
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    return _respond


def json_responder(payload: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    """A MockTransport handler that always answers with a JSON body."""
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            text=body,
            headers={"content-type": "application/json"},
            request=request,
        )

    return _respond


def xml_responder(body: str, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    """A MockTransport handler that always answers with an XML/RSS body."""

    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            text=body,
            headers={"content-type": "application/rss+xml; charset=utf-8"},
            request=request,
        )

    return _respond


def status_responder(status: int) -> Callable[[httpx.Request], httpx.Response]:
    """A MockTransport handler that answers with an empty response of ``status``."""

    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="", request=request)

    return _respond


def raise_responder(
    exc_factory: Callable[[], BaseException],
) -> Callable[[httpx.Request], httpx.Response]:
    """A MockTransport handler that raises the exception built by ``exc_factory``."""

    def _respond(request: httpx.Request) -> httpx.Response:
        raise exc_factory()

    return _respond


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """An ``httpx.Client`` whose transport is an in-memory MockTransport."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def run_async(coro: Any) -> Any:
    """Run a coroutine to completion (keeps the suite free of asyncio plugins)."""
    return asyncio.run(coro)


class FakeClock:
    """A monotonic-clock stand-in that only moves when told to."""

    def __init__(self, start: float = 1000.0) -> None:
        self._value = float(start)

    def now(self) -> float:
        return self._value

    def advance(self, seconds: float) -> float:
        self._value += float(seconds)
        return self._value

    def sleep(self, seconds: float) -> None:
        """``sleep`` replacement: advance instead of blocking real time."""
        self.advance(seconds)


class RecordingClock(FakeClock):
    """FakeClock that also records every requested sleep duration."""

    def __init__(self, start: float = 1000.0) -> None:
        super().__init__(start)
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.advance(seconds)


def frozen_time(seconds: float = 0.0) -> float:
    """Helper for ``time.monotonic`` patches: always returns the same instant."""
    return seconds


_ = time  # re-exported for tests that patch time.monotonic
