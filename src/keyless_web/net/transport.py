"""Network transport: direct first, explicit proxy as fallback.

All outgoing HTTP goes through :func:`run_with_fallback`, so the backend policy
lives in exactly one place:

1. ``direct`` -- a normal client that honours ``HTTP(S)_PROXY`` from the OS
   environment (``trust_env=True``), i.e. the "just work" path.
2. ``proxy`` -- only when ``MCP_WEB_PROXY`` is set. This is the escape hatch for
   networks where plain egress is filtered but a proxy is reachable. Writing the
   proxy as ``host:port`` is normalized to ``http://host:port``; using an IP
   there also works when local DNS is broken, because the proxy resolves names.

Only transport failures (timeouts, connect/DNS/TLS errors) move on to the next
backend. Once a response arrives, its HTTP status is the answer: 4xx/5xx raises
:class:`httpx.HTTPStatusError` immediately instead of silently retrying another
path. If every backend fails, :class:`AllBackendsFailed` aggregates the
per-backend errors so the caller can show why each path failed.

Requests to the same host are spaced by at least ``KEYLESS_WEB_MIN_INTERVAL``
seconds (default 1.0) -- polite by default, and it keeps scraped endpoints from
rate-limiting us out of the box.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

import httpx

from ..config import (
    DEFAULT_MIN_INTERVAL,
    PROXY_ENV,
    Settings,
    load_settings,
    normalize_proxy,
)
from ..errors import KeylessWebError, NetworkError, RateLimitedError

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Backend:
    """One way of reaching the network."""

    name: str
    proxy: str | None = None


class AllBackendsFailed(RuntimeError):
    """Every network backend failed.

    Attributes:
        errors: ``(backend_name, exception)`` pairs, in the order tried.
    """

    def __init__(self, errors: list[tuple[str, BaseException]] | None = None) -> None:
        self.errors: list[tuple[str, BaseException]] = list(errors or [])
        detail = "; ".join(f"{name}: {type(err).__name__}: {err}" for name, err in self.errors)
        message = f"all network backends failed ({detail})" if detail else "no network backend"
        super().__init__(message)

    @property
    def short(self) -> str:
        """The first backend's failure, suitable for a one-line message."""
        if not self.errors:
            return "no network backend available"
        name, err = self.errors[0]
        return f"{name} request failed: {type(err).__name__}: {err}"


def configured_proxy(env: Mapping[str, str] | None = None) -> str | None:
    """Normalized proxy URL from ``MCP_WEB_PROXY``, or ``None``."""
    if env is None:
        env = os.environ
    return normalize_proxy(env.get(PROXY_ENV) or "")


def backends(settings: Settings | None = None) -> list[Backend]:
    """Ordered backends to try: direct, then the configured proxy if any."""
    current = settings or load_settings()
    items = [Backend("direct", None)]
    if current.proxy_url:
        items.append(Backend("proxy", current.proxy_url))
    return items


class HostRateLimiter:
    """Enforce a minimum interval between requests to the same host.

    Args:
        min_interval: seconds to keep between two requests to one host.
            ``0`` disables the limiter.
        clock: monotonic time source (injectable for tests).
        sleep: sleeper (injectable for tests).

    The limiter is also a valid ``httpx`` request event hook, so it can be
    attached with ``httpx.Client(event_hooks={"request": [limiter]})``.
    """

    def __init__(
        self,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._sleep = sleep
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def min_interval(self) -> float:
        return self._min_interval

    def wait(self, host: str) -> float:
        """Block until ``host`` may be contacted again; return seconds slept."""
        if self._min_interval <= 0 or not host:
            return 0.0
        with self._lock:
            now = self._clock()
            last = self._last_seen.get(host)
            delay = 0.0 if last is None else self._min_interval - (now - last)
            if delay > 0:
                self._sleep(delay)
            else:
                delay = 0.0
            self._last_seen[host] = self._clock()
        return delay

    def __call__(self, request: httpx.Request) -> None:
        """``httpx`` request hook: wait for the request's host."""
        host = request.url.host
        if host:
            self.wait(host)

    hook = __call__

    def reset(self) -> None:
        with self._lock:
            self._last_seen.clear()


_LIMITER: HostRateLimiter | None = None
_LIMITER_LOCK = threading.Lock()


def default_limiter(min_interval: float | None = None) -> HostRateLimiter:
    """Process-wide limiter, built from ``KEYLESS_WEB_MIN_INTERVAL`` on first use."""
    global _LIMITER
    with _LIMITER_LOCK:
        if _LIMITER is None:
            interval = DEFAULT_MIN_INTERVAL if min_interval is None else min_interval
            _LIMITER = HostRateLimiter(interval)
        elif min_interval is not None and min_interval != _LIMITER.min_interval:
            _LIMITER = HostRateLimiter(min_interval)
        return _LIMITER


def reset_limiter() -> None:
    """Drop the process-wide limiter so the next call re-reads settings."""
    global _LIMITER
    with _LIMITER_LOCK:
        _LIMITER = None


def build_client(
    backend: Backend,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float | None = None,
) -> httpx.Client:
    """Create an ``httpx`` client for one backend.

    ``transport`` takes over the whole network path: ``httpx`` gives explicit
    mounts (which a ``proxy`` installs) precedence over a passed-in transport, so
    an injected transport and a proxy cannot coexist. When a transport is given
    it therefore wins, and ``backend.proxy`` is ignored -- which is what tests
    want (``httpx.MockTransport`` must see every backend) and what an advanced
    caller passing a custom transport expects anyway.
    """
    current = settings or load_settings()
    limiter = default_limiter(current.min_interval)
    kwargs: dict[str, object] = {
        "headers": {"User-Agent": current.user_agent},
        "timeout": current.timeout if timeout is None else timeout,
        "follow_redirects": True,
        "event_hooks": {"request": [limiter]},
    }
    if transport is not None:
        return httpx.Client(transport=transport, trust_env=False, **kwargs)  # type: ignore[arg-type]
    if backend.proxy:
        return httpx.Client(proxy=backend.proxy, trust_env=False, **kwargs)  # type: ignore[arg-type]
    return httpx.Client(trust_env=True, **kwargs)  # type: ignore[arg-type]


def run_with_fallback(
    call: Callable[[httpx.Client], T],
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float | None = None,
    backend_list: list[Backend] | None = None,
) -> T:
    """Run ``call(client)`` against each backend until one succeeds.

    Raises:
        AllBackendsFailed: every backend raised a transport error.
    """
    current = settings or load_settings()
    errors: list[tuple[str, BaseException]] = []
    for backend in backend_list if backend_list is not None else backends(current):
        try:
            with build_client(
                backend, settings=current, transport=transport, timeout=timeout
            ) as client:
                return call(client)
        except httpx.TransportError as exc:  # timeout / connect / DNS / TLS
            errors.append((backend.name, exc))
            continue
    raise AllBackendsFailed(errors)


def request_with_fallback(
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float | None = None,
) -> httpx.Response:
    """Perform one request with backend fallback, returning a 2xx response.

    Raises:
        AllBackendsFailed: no backend could deliver a response.
        httpx.HTTPStatusError: a backend delivered a 4xx/5xx response.
    """

    def _call(client: httpx.Client) -> httpx.Response:
        response = client.request(method, url, params=params, headers=headers)
        response.raise_for_status()
        return response

    return run_with_fallback(_call, settings=settings, transport=transport, timeout=timeout)


def is_retryable(exc: BaseException) -> bool:
    """Whether ``exc`` should move on to the next backend."""
    return isinstance(exc, httpx.TransportError)


def to_keyless_error(exc: BaseException) -> KeylessWebError:
    """Translate a transport/HTTP failure into the public error model."""
    if isinstance(exc, KeylessWebError):
        return exc
    if isinstance(exc, AllBackendsFailed):
        return NetworkError(str(exc), cause=exc)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        target = str(exc.request.url)
        if status == 429:
            return RateLimitedError(
                f"{target} answered HTTP 429 (too many requests)",
                cause=exc,
            )
        if status in (401, 403):
            return RateLimitedError(
                f"{target} answered HTTP {status} (the site refused the request)",
                hint=(
                    "The site blocks scripted access. Try another source for the "
                    "same fact, or fetch a different URL."
                ),
                cause=exc,
            )
        if status >= 500:
            return NetworkError(
                f"{target} answered HTTP {status} (server error)",
                hint="The remote service is failing; retry later or pick another source.",
                cause=exc,
            )
        return NetworkError(
            f"{target} answered HTTP {status}",
            hint="Check the URL for typos and that the page still exists.",
            cause=exc,
        )
    if isinstance(exc, httpx.TransportError):
        return NetworkError(
            f"network error: {type(exc).__name__}: {exc}",
            cause=exc,
        )
    return KeylessWebError(f"{type(exc).__name__}: {exc}", cause=exc)


def inject_system_trust() -> bool:
    """Load the OS certificate store into Python's TLS stack.

    Corporate TLS interception (and some desktop antivirus products) replaces
    certificates with ones only the OS store trusts; ``truststore`` makes
    ``httpx`` accept them without disabling verification. Returns ``True`` when
    injection succeeded.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
        return True
    except Exception:  # pragma: no cover - optional hardening
        return False


__all__ = [
    "AllBackendsFailed",
    "Backend",
    "HostRateLimiter",
    "backends",
    "build_client",
    "configured_proxy",
    "default_limiter",
    "inject_system_trust",
    "is_retryable",
    "request_with_fallback",
    "reset_limiter",
    "run_with_fallback",
    "to_keyless_error",
]
