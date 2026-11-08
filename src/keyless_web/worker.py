"""Long-lived JSON-lines worker: one process, many calls.

The MCP surface in :mod:`keyless_web.server` is a request/response protocol, so
it only fits hosts that speak MCP. A host that merely wants *search* and *fetch*
-- an editor plugin, a local agent runtime, a harness web-provider shim -- would
otherwise have to start an interpreter per call, which costs more than the
search itself once ``trafilatura`` is imported.

This module is that cheaper seam: the same engine and fetch code, reached over
stdin/stdout with one JSON object per line.

Protocol
--------
Request (one line, UTF-8)::

    {"id": 7, "method": "search", "params": {"query": "asyncio", "maxResults": 5}}
    {"id": 8, "method": "fetch", "params": {"url": "https://example.com"}}
    {"id": 9, "method": "ping"}
    {"id": 10, "method": "shutdown"}

Response (one line, UTF-8), matched to the request by ``id``::

    {"id": 7, "ok": true, "result": {...}}
    {"id": 7, "ok": false, "error": {"code": "KEYLESS_NETWORK",
                                     "message": "...", "hint": "..."}}

``id`` is echoed, never interpreted, so a caller may use integers or strings.
``result`` for ``search`` is ``{"query", "results": [{title, url, snippet,
engine, source?, date?}], "truncated"}``; for ``fetch`` it is ``{"url",
"statusCode", "text", "truncated"}``. A malformed line still gets an answer
(``id: null``, ``KEYLESS_BAD_REQUEST``) so the caller never hangs.

Requests run on a small thread pool, so a slow search does not block a fetch
issued while it is in flight. ``stdout`` carries protocol lines only: logs go to
``stderr``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import IO, Any

from . import __version__
from .config import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_RESULTS,
    MAX_MAX_CHARS,
    MAX_SEARCH_RESULTS,
    MIN_MAX_CHARS,
)
from .engines import available_engines
from .engines import search as run_search
from .errors import BadRequestError, KeylessWebError
from .fetch import fetch_document
from .net.transport import inject_system_trust, to_keyless_error

#: Version of the line protocol described above. A caller that cannot speak this
#: version should fall back to the MCP server instead of guessing.
PROTOCOL_VERSION = 1

#: Worker name reported by ``ping``.
WORKER_NAME = "keyless-web-worker"

#: Requests served concurrently; bounds the threads one worker process may hold.
DEFAULT_MAX_WORKERS = 4

_LOG = logging.getLogger("keyless_web.worker")


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    """Coerce ``value`` into ``[low, high]``, falling back to ``default``."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _string_param(params: Mapping[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BadRequestError(
            f"params.{key} must be a non-empty string",
            hint=f'Send {{"method": "...", "params": {{"{key}": "..."}}}}.',
        )
    return value


def _engines_param(params: Mapping[str, Any]) -> list[str] | None:
    value = params.get("engines")
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise BadRequestError(
            "params.engines must be a list of engine names",
            hint=f"Pass any of: {', '.join(available_engines())}.",
        )
    names: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise BadRequestError(
                "params.engines must contain only strings",
                hint=f"Pass any of: {', '.join(available_engines())}.",
            )
        names.append(item)
    return names or None


def run_search_request(params: Mapping[str, Any]) -> dict[str, Any]:
    """Serve one ``search`` request.

    Args:
        params: ``{"query": str, "maxResults"?: int, "engines"?: list[str]}``.

    Returns:
        ``{"query", "results", "truncated"}``, where ``truncated`` says the hit
        list filled the requested limit and may therefore be incomplete.

    Raises:
        BadRequestError: a parameter is missing or invalid.
        KeylessWebError: the query itself is unusable.
    """
    query = _string_param(params, "query")
    limit = _clamp(params.get("maxResults"), 1, MAX_SEARCH_RESULTS, DEFAULT_MAX_RESULTS)
    results = run_search(query, engines=_engines_param(params), max_results=limit)
    return {
        "query": query,
        "results": [item.to_dict() for item in results],
        "truncated": len(results) >= limit,
    }


def run_fetch_request(params: Mapping[str, Any]) -> dict[str, Any]:
    """Serve one ``fetch`` request.

    Args:
        params: ``{"url": str, "maxChars"?: int, "includeLinks"?: bool}``.

    Returns:
        ``{"url", "statusCode", "text", "truncated"}``. A non-2xx answer is a
        result, not an error: the caller decides what to show.

    Raises:
        BadRequestError: ``url`` is missing or empty.
        KeylessWebError: the URL is not http(s), or the network failed.
    """
    url = _string_param(params, "url")
    max_chars = _clamp(params.get("maxChars"), MIN_MAX_CHARS, MAX_MAX_CHARS, DEFAULT_MAX_CHARS)
    document = fetch_document(
        url,
        max_chars=max_chars,
        include_links=bool(params.get("includeLinks", False)),
        allow_error_status=True,
    )
    return {
        "url": document.url,
        "statusCode": document.status_code,
        "text": document.text,
        "truncated": document.truncated,
    }


def dispatch(method: str, params: Mapping[str, Any]) -> Any:
    """Run one method and return its result payload.

    Raises:
        BadRequestError: the method is unknown or its parameters are invalid.
        KeylessWebError: the underlying call failed.
    """
    if method == "ping":
        return {
            "name": WORKER_NAME,
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "engines": list(available_engines()),
        }
    if method == "search":
        return run_search_request(params)
    if method == "fetch":
        return run_fetch_request(params)
    raise BadRequestError(
        f"unknown method {method!r}",
        hint="Use one of: ping, search, fetch, shutdown.",
    )


def _error_payload(exc: BaseException) -> dict[str, str]:
    """Map any failure onto the public ``KEYLESS_*`` error model."""
    if isinstance(exc, KeylessWebError):
        return exc.to_dict()
    error = to_keyless_error(exc)
    _LOG.exception("worker: unhandled failure")
    return error.to_dict()


def _parse(line: str) -> tuple[Any, str, Mapping[str, Any]]:
    """Split one request line into ``(id, method, params)``.

    Raises:
        JsonError: the line is not a JSON object with a string method, in which
            case the caller answers with an error instead of dying.
    """
    try:
        payload = json.loads(line)
    except ValueError as exc:
        raise JsonError(f"malformed JSON request: {exc}") from None
    if not isinstance(payload, dict):
        raise JsonError("a request must be a JSON object")
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        raise JsonError("a request needs a non-empty string method")
    params = payload.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise JsonError("params must be a JSON object")
    return payload.get("id"), method, params


class JsonError(ValueError):
    """A request line this worker cannot parse."""


class Worker:
    """Serve JSON-line requests from ``stdin`` until EOF or ``shutdown``.

    Args:
        reader: text stream carrying request lines.
        writer: text stream that receives response lines.
        max_workers: requests served concurrently.
        ensure_ascii: escape non-ASCII in responses (safe on a stream whose
            encoding is not UTF-8).
    """

    def __init__(
        self,
        reader: IO[str],
        writer: IO[str],
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
        ensure_ascii: bool = False,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._ensure_ascii = ensure_ascii
        self._write_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)), thread_name_prefix="keyless-worker"
        )

    # ------------------------------------------------------------------ output

    def _write(self, message: Mapping[str, Any]) -> None:
        text = json.dumps(message, ensure_ascii=self._ensure_ascii)
        with self._write_lock:
            self._writer.write(text + "\n")
            self._writer.flush()

    def _respond_ok(self, request_id: Any, result: Any) -> None:
        self._write({"id": request_id, "ok": True, "result": result})

    def _respond_error(self, request_id: Any, error: Mapping[str, str]) -> None:
        self._write({"id": request_id, "ok": False, "error": dict(error)})

    # ----------------------------------------------------------------- requests

    def _run(self, request_id: Any, method: str, params: Mapping[str, Any]) -> None:
        """Dispatch one parsed request and answer it. Never raises."""
        try:
            self._respond_ok(request_id, dispatch(method, params))
        except Exception as exc:
            self._respond_error(request_id, _error_payload(exc))

    def serve(self) -> int:
        """Read request lines until EOF or ``shutdown``; return an exit code."""
        futures = []
        for line in self._reader:
            statement = line.strip()
            if not statement:
                continue
            try:
                request_id, method, params = _parse(statement)
            except JsonError as exc:
                self._respond_error(
                    None, {"code": "KEYLESS_BAD_REQUEST", "message": str(exc), "hint": ""}
                )
                continue
            if method == "shutdown":
                self._respond_ok(request_id, {"stopped": True})
                break
            futures.append(self._pool.submit(self._run, request_id, method, params))
        for future in futures:
            future.result()
        self._pool.shutdown(wait=True)
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point behind ``python -m keyless_web.worker``."""
    parser = argparse.ArgumentParser(
        prog="keyless-web-worker",
        description=(
            "Serve keyless web search and page reading over stdin/stdout "
            "JSON lines (one request, one response, matched by id)."
        ),
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=f"requests served concurrently (default {DEFAULT_MAX_WORKERS}).",
    )
    parser.add_argument("--version", action="version", version=f"keyless-web-worker {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="keyless-web-worker: %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Same reasoning as the MCP entry point: corporate TLS interception and
    # desktop antivirus both break the bundled CA set.
    inject_system_trust()

    # stdout is protocol-only and must be UTF-8 regardless of the console code
    # page; when it cannot be reconfigured, escaping non-ASCII keeps it valid.
    ensure_ascii = True
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
        ensure_ascii = False
    except (AttributeError, ValueError):  # pragma: no cover - exotic streams
        _LOG.warning("worker: stdout is not UTF-8; escaping non-ASCII responses")

    return Worker(
        sys.stdin,
        sys.stdout,
        max_workers=args.max_workers,
        ensure_ascii=ensure_ascii,
    ).serve()


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())


__all__ = [
    "PROTOCOL_VERSION",
    "WORKER_NAME",
    "JsonError",
    "Worker",
    "dispatch",
    "main",
    "run_fetch_request",
    "run_search_request",
]
