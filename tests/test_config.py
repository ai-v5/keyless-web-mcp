"""Environment-driven configuration: defaults, parsing and the settings singleton."""

from __future__ import annotations

import pytest

from keyless_web.config import (
    DEFAULT_ENGINES,
    DEFAULT_FETCH_CACHE_TTL,
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_RESULTS,
    DEFAULT_MIN_INTERVAL,
    DEFAULT_SEARCH_CACHE_TTL,
    DEFAULT_TIMEOUT,
    DEFAULT_USER_AGENT,
    ENGINE_NAMES,
    MAX_MAX_CHARS,
    MAX_SEARCH_RESULTS,
    MIN_MAX_CHARS,
    Settings,
    load_settings,
    normalize_proxy,
    parse_engines,
    reload_settings,
    settings,
)


def test_defaults_when_the_environment_is_empty() -> None:
    config = load_settings({})

    assert config.proxy is None
    assert config.proxy_url is None
    assert config.engines == DEFAULT_ENGINES
    assert config.min_interval == DEFAULT_MIN_INTERVAL
    assert config.timeout == DEFAULT_TIMEOUT
    assert config.max_results == DEFAULT_MAX_RESULTS
    assert config.max_chars == DEFAULT_MAX_CHARS
    assert config.search_cache_ttl == DEFAULT_SEARCH_CACHE_TTL
    assert config.fetch_cache_ttl == DEFAULT_FETCH_CACHE_TTL
    assert config.user_agent == DEFAULT_USER_AGENT


def test_bing_is_opt_in_only() -> None:
    """Bing needs cookies, so it must not be part of the default engine list."""
    assert "bing" not in DEFAULT_ENGINES
    assert DEFAULT_ENGINES == ("duckduckgo", "google", "wikipedia")
    assert "bing" in ENGINE_NAMES


def test_engine_names_are_the_public_enum() -> None:
    assert ENGINE_NAMES == ("bing", "duckduckgo", "google", "wikipedia")


def test_hard_caps() -> None:
    assert MAX_SEARCH_RESULTS == 20
    assert MIN_MAX_CHARS == 500
    assert MAX_MAX_CHARS == 100_000
    assert MIN_MAX_CHARS < DEFAULT_MAX_CHARS < MAX_MAX_CHARS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("127.0.0.1:8888", "http://127.0.0.1:8888"),
        ("http://127.0.0.1:8888", "http://127.0.0.1:8888"),
        ("https://proxy.example:3128", "https://proxy.example:3128"),
        (" 127.0.0.1:8888 ", "http://127.0.0.1:8888"),
        ("", None),
        ("   ", None),
    ],
)
def test_normalize_proxy(raw: str, expected: str | None) -> None:
    assert normalize_proxy(raw) == expected


def test_proxy_env_is_normalized() -> None:
    config = load_settings({"MCP_WEB_PROXY": "127.0.0.1:8888"})

    assert config.proxy == "http://127.0.0.1:8888"
    assert config.proxy_url == "http://127.0.0.1:8888"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("google", ("google",)),
        ("bing, google", ("bing", "google")),
        ("BING", ("bing",)),
        ("google  google", ("google",)),
        ("google,bing,nosuchengine", ("google", "bing")),
        ("", DEFAULT_ENGINES),
        ("nosuchengine", DEFAULT_ENGINES),
    ],
)
def test_parse_engines(raw: str, expected: tuple[str, ...]) -> None:
    assert parse_engines(raw) == expected


def test_numbers_come_from_the_environment() -> None:
    config = load_settings(
        {
            "KEYLESS_WEB_MIN_INTERVAL": "0",
            "KEYLESS_WEB_TIMEOUT": "2.5",
            "KEYLESS_WEB_CACHE_TTL": "60",
            "KEYLESS_WEB_CACHE_TTL_FETCH": "120",
            "KEYLESS_WEB_USER_AGENT": "keyless-tests/2.0",
        }
    )

    assert config.min_interval == 0.0
    assert config.timeout == 2.5
    assert config.search_cache_ttl == 60.0
    assert config.fetch_cache_ttl == 120.0
    assert config.user_agent == "keyless-tests/2.0"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KEYLESS_WEB_TIMEOUT", "not-a-number"),
        ("KEYLESS_WEB_MIN_INTERVAL", "-3"),
        ("KEYLESS_WEB_CACHE_TTL", ""),
    ],
)
def test_invalid_numbers_fall_back_to_defaults(name: str, value: str) -> None:
    config = load_settings({name: value})

    assert config.timeout == DEFAULT_TIMEOUT
    assert config.min_interval == DEFAULT_MIN_INTERVAL
    assert config.search_cache_ttl == DEFAULT_SEARCH_CACHE_TTL


def test_settings_are_immutable() -> None:
    config = load_settings({})

    with pytest.raises(AttributeError):
        config.timeout = 1.0  # type: ignore[misc]


def test_settings_singleton_is_cached_and_reloadable(monkeypatch: pytest.MonkeyPatch) -> None:
    first = settings()

    assert settings() is first
    assert isinstance(first, Settings)

    monkeypatch.setenv("KEYLESS_WEB_TIMEOUT", "3")
    reloaded = reload_settings()

    assert reloaded.timeout == 3.0
    assert reloaded is not first
    assert settings() is reloaded
