"""Configuration: every knob is read from the environment, once.

The package ships zero configuration files on purpose -- a MCP client launch
command is the only place a user can set environment variables, so reading the
environment is the least surprising interface.

Supported variables
-------------------
``MCP_WEB_PROXY``
    Explicit proxy used as the fallback network backend, ``host:port`` or a full
    URL (``http://127.0.0.1:7890``). Direct connections are always tried first.
``KEYLESS_WEB_ENGINES``
    Comma separated default engine list. Default:
    ``google_web,duckduckgo,bing,google``.
``KEYLESS_WEB_MIN_INTERVAL``
    Minimum seconds between two requests to the same host. Default ``1.0``;
    ``0`` disables the rate limiter.
``KEYLESS_WEB_CACHE_TTL``
    Search cache TTL in seconds. Default ``300``; ``0`` disables caching.
``KEYLESS_WEB_CACHE_TTL_FETCH``
    Page cache TTL in seconds. Default ``900``; ``0`` disables caching.
``KEYLESS_WEB_USER_AGENT``
    User-Agent header sent with every request.
``KEYLESS_WEB_TIMEOUT``
    Per-request timeout in seconds. Default ``15``.
``KEYLESS_WEB_BROWSER``
    Browser executable used by browser-backed engines. Default: auto-detected
    Chrome/Edge/Chromium; ``off`` disables the browser entirely.
``KEYLESS_WEB_BROWSER_TIMEOUT``
    Wall-clock budget for one rendered page, in seconds. Default ``25``.
``KEYLESS_WEB_BROWSER_WAIT``
    Virtual time budget handed to the browser, in milliseconds. Default ``6000``;
    raise it on a slow link and lower it to spend less time per search.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache

PROXY_ENV = "MCP_WEB_PROXY"
ENGINES_ENV = "KEYLESS_WEB_ENGINES"
MIN_INTERVAL_ENV = "KEYLESS_WEB_MIN_INTERVAL"
CACHE_TTL_ENV = "KEYLESS_WEB_CACHE_TTL"
FETCH_CACHE_TTL_ENV = "KEYLESS_WEB_CACHE_TTL_FETCH"
USER_AGENT_ENV = "KEYLESS_WEB_USER_AGENT"
TIMEOUT_ENV = "KEYLESS_WEB_TIMEOUT"
BROWSER_ENV = "KEYLESS_WEB_BROWSER"
BROWSER_TIMEOUT_ENV = "KEYLESS_WEB_BROWSER_TIMEOUT"
BROWSER_WAIT_ENV = "KEYLESS_WEB_BROWSER_WAIT"

#: Engine names accepted by the ``engines`` tool argument.
ENGINE_NAMES: tuple[str, ...] = (
    "bing",
    "duckduckgo",
    "firecrawl",
    "google",
    "google_web",
    "wikipedia",
)

#: Engine order used when the caller does not pass ``engines``.
#:
#: ``google_web`` renders ``google.com/search`` in a headless browser, so it is
#: the only engine that reads the real web index; ``duckduckgo`` and ``bing`` are
#: plain HTTP backends that work without a browser, and ``google`` is Google News
#: RSS (see :mod:`keyless_web.engines.google_news`). Results from all of them are
#: interleaved round-robin, so each backend contributes what it ranks highest.
DEFAULT_ENGINES: tuple[str, ...] = ("google_web", "duckduckgo", "bing", "google")

DEFAULT_TIMEOUT = 15.0
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_SEARCH_CACHE_TTL = 300.0
DEFAULT_FETCH_CACHE_TTL = 900.0
DEFAULT_BROWSER_TIMEOUT = 25.0
DEFAULT_BROWSER_WAIT_MS = 6000
DEFAULT_MAX_RESULTS = 10
DEFAULT_MAX_CHARS = 20_000

#: Hard caps enforced by the tool layer, whatever the caller asks for.
MAX_SEARCH_RESULTS = 20
MIN_MAX_CHARS = 500
MAX_MAX_CHARS = 100_000

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _get(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _get(env, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _get(env, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def parse_engines(raw: str) -> tuple[str, ...]:
    """Parse a comma/space separated engine list, dropping unknown names.

    An empty or fully invalid list falls back to :data:`DEFAULT_ENGINES` so a
    typo in ``KEYLESS_WEB_ENGINES`` cannot leave the server with no engines.
    """
    names: list[str] = []
    for chunk in raw.replace(",", " ").split():
        name = chunk.strip().lower()
        if name in ENGINE_NAMES and name not in names:
            names.append(name)
    return tuple(names) or DEFAULT_ENGINES


def normalize_proxy(raw: str) -> str | None:
    """Normalize ``host:port`` to ``http://host:port``; ``None`` when unset."""
    value = (raw or "").strip()
    if not value:
        return None
    return value if "://" in value else f"http://{value}"


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable snapshot of the runtime configuration."""

    proxy: str | None = None
    engines: tuple[str, ...] = DEFAULT_ENGINES
    min_interval: float = DEFAULT_MIN_INTERVAL
    search_cache_ttl: float = DEFAULT_SEARCH_CACHE_TTL
    fetch_cache_ttl: float = DEFAULT_FETCH_CACHE_TTL
    user_agent: str = field(default=DEFAULT_USER_AGENT)
    timeout: float = DEFAULT_TIMEOUT
    max_results: int = DEFAULT_MAX_RESULTS
    max_chars: int = DEFAULT_MAX_CHARS
    browser: str | None = None
    browser_timeout: float = DEFAULT_BROWSER_TIMEOUT
    browser_wait_ms: int = DEFAULT_BROWSER_WAIT_MS

    @property
    def proxy_url(self) -> str | None:
        """Normalized proxy URL, or ``None`` when direct-only."""
        return normalize_proxy(self.proxy or "")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from a mapping (defaults to ``os.environ``).

    Pure function: pass a dict in tests to avoid touching global state.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    raw_engines = _get(source, ENGINES_ENV)
    return Settings(
        proxy=normalize_proxy(_get(source, PROXY_ENV)),
        engines=parse_engines(raw_engines) if raw_engines else DEFAULT_ENGINES,
        min_interval=_env_float(source, MIN_INTERVAL_ENV, DEFAULT_MIN_INTERVAL),
        search_cache_ttl=_env_float(source, CACHE_TTL_ENV, DEFAULT_SEARCH_CACHE_TTL),
        fetch_cache_ttl=_env_float(source, FETCH_CACHE_TTL_ENV, DEFAULT_FETCH_CACHE_TTL),
        user_agent=_get(source, USER_AGENT_ENV) or DEFAULT_USER_AGENT,
        timeout=_env_float(source, TIMEOUT_ENV, DEFAULT_TIMEOUT),
        max_results=DEFAULT_MAX_RESULTS,
        max_chars=DEFAULT_MAX_CHARS,
        browser=_get(source, BROWSER_ENV) or None,
        browser_timeout=_env_float(source, BROWSER_TIMEOUT_ENV, DEFAULT_BROWSER_TIMEOUT),
        browser_wait_ms=_env_int(source, BROWSER_WAIT_ENV, DEFAULT_BROWSER_WAIT_MS),
    )


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Process-wide settings snapshot (cached; see :func:`reload_settings`)."""
    return load_settings()


def reload_settings() -> Settings:
    """Drop the cached snapshot and read the environment again."""
    settings.cache_clear()
    return settings()


__all__ = [
    "BROWSER_ENV",
    "BROWSER_TIMEOUT_ENV",
    "BROWSER_WAIT_ENV",
    "CACHE_TTL_ENV",
    "DEFAULT_BROWSER_TIMEOUT",
    "DEFAULT_BROWSER_WAIT_MS",
    "DEFAULT_ENGINES",
    "DEFAULT_FETCH_CACHE_TTL",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_MIN_INTERVAL",
    "DEFAULT_SEARCH_CACHE_TTL",
    "DEFAULT_TIMEOUT",
    "DEFAULT_USER_AGENT",
    "ENGINES_ENV",
    "ENGINE_NAMES",
    "FETCH_CACHE_TTL_ENV",
    "MAX_MAX_CHARS",
    "MAX_SEARCH_RESULTS",
    "MIN_INTERVAL_ENV",
    "MIN_MAX_CHARS",
    "PROXY_ENV",
    "TIMEOUT_ENV",
    "USER_AGENT_ENV",
    "Settings",
    "load_settings",
    "normalize_proxy",
    "parse_engines",
    "reload_settings",
    "settings",
]
