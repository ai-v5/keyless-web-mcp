"""Network policy: direct first, configured proxy as fallback.

Every case runs on ``httpx.MockTransport``; ``tests/conftest.py`` fails the test
if anything tries to open a real socket.
"""

from __future__ import annotations

import httpx
import pytest

from keyless_web.config import load_settings
from keyless_web.errors import (
    KEYLESS_BAD_REQUEST,
    KEYLESS_NETWORK,
    KEYLESS_RATE_LIMITED,
    KeylessWebError,
    NetworkError,
    RateLimitedError,
)
from keyless_web.net.transport import (
    AllBackendsFailed,
    Backend,
    backends,
    build_client,
    configured_proxy,
    is_retryable,
    request_with_fallback,
    run_with_fallback,
    to_keyless_error,
)

TARGET = "https://example.com/resource"


def _settings(**extra: str):
    """Settings with the rate limiter off so tests never sleep."""
    return load_settings({"KEYLESS_WEB_MIN_INTERVAL": "0", **extra})


def _proxy_settings(**extra: str):
    return _settings(MCP_WEB_PROXY="127.0.0.1:8888", **extra)


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", TARGET)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


# --------------------------------------------------------------------------- happy path


def test_direct_backend_returns_the_response() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok", request=request)

    response = request_with_fallback(
        "GET", TARGET, settings=_settings(), transport=httpx.MockTransport(handler)
    )

    assert response.status_code == 200
    assert response.text == "ok"
    assert len(seen) == 1


def test_run_with_fallback_returns_the_call_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    value = run_with_fallback(
        lambda client: 42, settings=_settings(), transport=httpx.MockTransport(handler)
    )

    assert value == 42


# --------------------------------------------------------------------------- fallback


def test_direct_failure_falls_back_to_the_configured_proxy() -> None:
    """A transport error on ``direct`` must retry once through ``MCP_WEB_PROXY``."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, text="via proxy", request=request)

    response = request_with_fallback(
        "GET",
        TARGET,
        settings=_proxy_settings(),
        transport=httpx.MockTransport(handler),
    )

    assert response.status_code == 200
    assert response.text == "via proxy"
    assert len(seen) == 2


def test_fallback_loop_visits_backends_in_order() -> None:
    """Backend iteration itself, independent of how a proxy client is built."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            raise httpx.ConnectTimeout("connect timed out")
        return httpx.Response(200, text="second", request=request)

    value = run_with_fallback(
        lambda client: client.get(TARGET).text,
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        backend_list=[Backend("direct", None), Backend("proxy", None)],
    )

    assert value == "second"
    assert len(seen) == 2


@pytest.mark.parametrize("exception", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_all_backends_failed_aggregates_every_backend(exception: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception

    with pytest.raises(AllBackendsFailed) as excinfo:
        request_with_fallback(
            "GET", TARGET, settings=_proxy_settings(), transport=httpx.MockTransport(handler)
        )

    error = excinfo.value
    assert [name for name, _ in error.errors] == ["direct", "proxy"]
    assert all(isinstance(err, Exception) for _, err in error.errors)
    message = str(error)
    assert "direct" in message and "proxy" in message
    assert type(exception).__name__ in message
    assert error.short.startswith("direct request failed")
    assert type(exception).__name__ in error.short


def test_direct_only_failure_names_one_backend() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(AllBackendsFailed) as excinfo:
        request_with_fallback(
            "GET", TARGET, settings=_settings(), transport=httpx.MockTransport(handler)
        )

    assert [name for name, _ in excinfo.value.errors] == ["direct"]
    assert "proxy" not in str(excinfo.value)


def test_all_backends_failed_without_errors_still_has_a_message() -> None:
    error = AllBackendsFailed()

    assert error.errors == []
    assert str(error) == "no network backend"
    assert error.short == "no network backend available"


# --------------------------------------------------------------------------- status codes


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 503])
def test_http_error_is_raised_without_retrying_another_backend(status: int) -> None:
    """Once a response arrives its status is the answer: no silent proxy retry."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, text="nope", request=request)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        request_with_fallback(
            "GET", TARGET, settings=_proxy_settings(), transport=httpx.MockTransport(handler)
        )

    assert excinfo.value.response.status_code == status
    assert len(seen) == 1


def test_non_transport_exception_is_not_swallowed() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise ValueError("programmer error")

    with pytest.raises(ValueError):
        request_with_fallback(
            "GET", TARGET, settings=_proxy_settings(), transport=httpx.MockTransport(handler)
        )

    assert calls["count"] == 1


# --------------------------------------------------------------------------- backends


def test_backends_start_with_direct_then_the_normalized_proxy() -> None:
    configured = backends(_proxy_settings())

    assert [backend.name for backend in configured] == ["direct", "proxy"]
    assert configured[0].proxy is None
    assert configured[1].proxy == "http://127.0.0.1:8888"


def test_backends_without_proxy_are_direct_only() -> None:
    assert [backend.name for backend in backends(_settings())] == ["direct"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("127.0.0.1:8888", "http://127.0.0.1:8888"),
        ("http://127.0.0.1:8888", "http://127.0.0.1:8888"),
        ("https://proxy.example:3128", "https://proxy.example:3128"),
        ("", None),
        ("   ", None),
    ],
)
def test_configured_proxy_normalizes_host_port(raw: str, expected: str | None) -> None:
    assert configured_proxy({"MCP_WEB_PROXY": raw}) == expected


def test_configured_proxy_is_none_when_unset() -> None:
    assert configured_proxy({}) is None


def test_build_client_applies_settings() -> None:
    settings = _settings(KEYLESS_WEB_USER_AGENT="keyless-tests/1.0", KEYLESS_WEB_TIMEOUT="7")

    client = build_client(Backend("direct", None), settings=settings)

    try:
        assert client.headers["user-agent"] == "keyless-tests/1.0"
        assert client.timeout.read == 7.0
        assert client.follow_redirects is True
    finally:
        client.close()


# --------------------------------------------------------------------------- error mapping


@pytest.mark.parametrize(
    ("exc", "expected_type", "expected_code"),
    [
        (_status_error(429), RateLimitedError, KEYLESS_RATE_LIMITED),
        (_status_error(403), RateLimitedError, KEYLESS_RATE_LIMITED),
        (_status_error(500), NetworkError, KEYLESS_NETWORK),
        (_status_error(404), NetworkError, KEYLESS_NETWORK),
        (
            AllBackendsFailed([("direct", httpx.ConnectError("refused"))]),
            NetworkError,
            KEYLESS_NETWORK,
        ),
        (ValueError("boom"), KeylessWebError, KEYLESS_BAD_REQUEST),
    ],
)
def test_to_keyless_error_maps_failures(
    exc: Exception, expected_type: type, expected_code: str
) -> None:
    error = to_keyless_error(exc)

    assert isinstance(error, expected_type)
    assert error.code == expected_code
    assert error.to_text().startswith(f"error[{expected_code}]:")


def test_to_keyless_error_keeps_an_existing_keyless_error() -> None:
    original = NetworkError("already mapped")

    assert to_keyless_error(original) is original


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (httpx.ConnectError("refused"), True),
        (httpx.ConnectTimeout("timeout"), True),
        (httpx.ReadTimeout("slow"), True),
        (_status_error(500), False),
        (ValueError("boom"), False),
    ],
)
def test_is_retryable(exc: Exception, retryable: bool) -> None:
    assert is_retryable(exc) is retryable
