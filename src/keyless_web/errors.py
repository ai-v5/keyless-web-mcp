"""Error model shared by the search engines and the page fetcher.

Every failure that reaches the model carries a stable ``KEYLESS_*`` code plus a
hint describing what to do next, so a failed call is actionable instead of an
opaque ``error: ...`` line. The codes are part of the public contract.
"""

from __future__ import annotations

KEYLESS_NETWORK = "KEYLESS_NETWORK"
"""No network path could deliver a response (DNS, connect, TLS, timeout)."""

KEYLESS_PARSE = "KEYLESS_PARSE"
"""A response arrived but could not be parsed into results or text."""

KEYLESS_RATE_LIMITED = "KEYLESS_RATE_LIMITED"
"""The remote site asked us to slow down (HTTP 429) or refused the request."""

KEYLESS_BAD_URL = "KEYLESS_BAD_URL"
"""The URL is not an absolute ``http://`` or ``https://`` URL."""

KEYLESS_BAD_REQUEST = "KEYLESS_BAD_REQUEST"
"""Invalid arguments: unknown engine name, empty query, out-of-range value."""

KEYLESS_BROWSER = "KEYLESS_BROWSER"
"""A browser-rendered source failed: no browser, a crash, or a render timeout."""

ERROR_CODES: tuple[str, ...] = (
    KEYLESS_NETWORK,
    KEYLESS_PARSE,
    KEYLESS_RATE_LIMITED,
    KEYLESS_BAD_URL,
    KEYLESS_BAD_REQUEST,
    KEYLESS_BROWSER,
)


class KeylessWebError(Exception):
    """Base class for every error raised by this package.

    Attributes:
        code: one of the ``KEYLESS_*`` constants.
        message: what went wrong, phrased for a language model to read.
        hint: how to fix it or what to try instead.
    """

    code: str = KEYLESS_BAD_REQUEST
    hint: str = ""

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if hint is not None:
            self.hint = hint
        self.cause = cause

    def to_text(self) -> str:
        """Render the error as a single actionable line for the model."""
        text = f"error[{self.code}]: {self.message}"
        if self.hint:
            text = f"{text}. Hint: {self.hint}"
        return text

    def to_dict(self) -> dict[str, str]:
        """Machine-readable form, convenient for non-MCP callers."""
        return {"code": self.code, "message": self.message, "hint": self.hint}

    def __str__(self) -> str:
        return self.to_text()


class NetworkError(KeylessWebError):
    """Every transport backend failed, or the server answered with an error."""

    code = KEYLESS_NETWORK
    hint = (
        "Check connectivity to the host, then retry once. If this host is only "
        "reachable through a proxy, set MCP_WEB_PROXY (for example "
        "http://127.0.0.1:7890) and start the server again."
    )


class ParseError(KeylessWebError):
    """The response payload was structurally unusable."""

    code = KEYLESS_PARSE
    hint = (
        "The site probably changed its markup or served a block page. Retry with "
        "another engine, or fetch a specific URL with web_fetch instead."
    )


class RateLimitedError(KeylessWebError):
    """The remote host is throttling us."""

    code = KEYLESS_RATE_LIMITED
    hint = (
        "Wait a few seconds before retrying, or raise KEYLESS_WEB_MIN_INTERVAL "
        "(seconds between requests to the same host) to slow all requests down."
    )


class BadUrlError(KeylessWebError):
    """The supplied URL is not an absolute http(s) URL."""

    code = KEYLESS_BAD_URL
    hint = (
        "Pass an absolute http:// or https:// URL, for example "
        "https://docs.python.org/3/library/asyncio.html."
    )


class BadRequestError(KeylessWebError):
    """The call arguments are invalid."""

    code = KEYLESS_BAD_REQUEST


class BrowserUnavailableError(KeylessWebError):
    """A browser-rendered source could not be rendered."""

    code = KEYLESS_BROWSER
    hint = (
        "Install Chrome or Edge, or point KEYLESS_WEB_BROWSER at an executable. "
        "Engines that do not need a browser keep working."
    )


__all__ = [
    "ERROR_CODES",
    "KEYLESS_BAD_REQUEST",
    "KEYLESS_BAD_URL",
    "KEYLESS_BROWSER",
    "KEYLESS_NETWORK",
    "KEYLESS_PARSE",
    "KEYLESS_RATE_LIMITED",
    "BadRequestError",
    "BadUrlError",
    "BrowserUnavailableError",
    "KeylessWebError",
    "NetworkError",
    "ParseError",
    "RateLimitedError",
]
