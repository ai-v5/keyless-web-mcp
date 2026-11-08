"""Page fetching: readability extraction, truncation, URL validation, caching."""

from __future__ import annotations

import httpx
import pytest

from keyless_web.config import load_settings
from keyless_web.errors import (
    KEYLESS_BAD_URL,
    KEYLESS_NETWORK,
    KEYLESS_RATE_LIMITED,
    BadUrlError,
    NetworkError,
    RateLimitedError,
)
from keyless_web.fetch import fetch_document, fetch_page
from keyless_web.fetch.extract import extract_page, is_http_url
from support import html_responder, json_responder, load_fixture

ARTICLE = load_fixture("article_zh.html")
PAGE_URL = "https://example.com/cache-post"


def _settings(**extra: str):
    return load_settings({"KEYLESS_WEB_MIN_INTERVAL": "0", **extra})


# --------------------------------------------------------------------------- extraction


def test_extracts_the_chinese_article_body() -> None:
    text = extract_page(ARTICLE, PAGE_URL)

    assert "分布式缓存的三种失效策略" in text
    assert "缓存击穿" in text
    assert "监控指标" in text


def test_renders_a_title_and_url_header() -> None:
    text = extract_page(ARTICLE, PAGE_URL)
    lines = text.splitlines()

    assert lines[0].startswith("Title: ")
    assert "分布式缓存的三种失效策略" in lines[0]
    assert f"URL: {PAGE_URL}" in lines


def test_boilerplate_and_script_content_are_dropped() -> None:
    text = extract_page(ARTICLE, PAGE_URL)

    assert "script-should-not-be-extracted" not in text
    assert "版权所有" not in text
    assert "Cookie" not in text
    assert "登录" not in text


def test_max_chars_truncates_with_an_explicit_marker() -> None:
    full = extract_page(ARTICLE, PAGE_URL)
    truncated = extract_page(ARTICLE, PAGE_URL, max_chars=120)

    assert len(full) > 120
    assert truncated.startswith(full[:120])
    assert truncated.endswith("[truncated at 120 chars]")
    assert len(truncated) < len(full)


def test_max_chars_above_the_document_size_changes_nothing() -> None:
    full = extract_page(ARTICLE, PAGE_URL)

    assert extract_page(ARTICLE, PAGE_URL, max_chars=1_000_000) == full


def test_include_links_lists_absolute_targets() -> None:
    text = extract_page(ARTICLE, PAGE_URL, include_links=True)

    assert "Links:" in text
    assert "- https://example.com/cache-design" in text
    assert "- https://example.com/about" in text


def test_links_are_absent_by_default() -> None:
    assert "Links:" not in extract_page(ARTICLE, PAGE_URL)


def test_extraction_works_without_a_document_url() -> None:
    text = extract_page(ARTICLE)

    assert "缓存击穿" in text
    assert "URL:" not in text


def test_html_without_article_text_falls_back_to_the_body() -> None:
    text = extract_page("<html><body><p>plain paragraph</p></body></html>")

    assert "plain paragraph" in text


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/a", True),
        ("http://127.0.0.1:8080/a", True),
        ("ftp://example.com/a", False),
        ("file:///C:/Windows/win.ini", False),
        ("javascript:alert(1)", False),
        ("example.com/no-scheme", False),
        ("", False),
    ],
)
def test_is_http_url(url: str, expected: bool) -> None:
    assert is_http_url(url) is expected


# --------------------------------------------------------------------------- fetching


def test_fetch_page_returns_extracted_text() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return html_responder(ARTICLE)(request)

    text = fetch_page(PAGE_URL, settings=_settings(), transport=httpx.MockTransport(handler))

    assert "缓存击穿" in text
    assert len(seen) == 1
    assert seen[0].url == httpx.URL(PAGE_URL)


def test_json_response_is_pretty_printed() -> None:
    payload = {"title": "示例数据", "items": [1, 2, 3]}
    text = fetch_page(
        "https://example.com/api.json",
        settings=_settings(),
        transport=httpx.MockTransport(json_responder(payload)),
    )

    assert text.startswith("{")
    assert '"title": "示例数据"' in text
    assert '"items"' in text


def test_plain_text_response_is_returned_verbatim() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="plain body",
            headers={"content-type": "text/plain"},
            request=request,
        )

    text = fetch_page(
        "https://example.com/robots.txt",
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )

    assert text == "plain body"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file.txt",
        "file:///C:/Windows/win.ini",
        "javascript:alert(1)",
        "data:text/html,<b>x</b>",
        "example.com/no-scheme",
        "",
        "   ",
    ],
)
def test_non_http_urls_are_rejected_before_any_request(url: str) -> None:
    called: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200, text="should not happen", request=request)

    with pytest.raises(BadUrlError) as excinfo:
        fetch_page(url, settings=_settings(), transport=httpx.MockTransport(handler))

    assert excinfo.value.code == KEYLESS_BAD_URL
    assert excinfo.value.to_text().startswith("error[KEYLESS_BAD_URL]:")
    assert called == []


@pytest.mark.parametrize(
    ("status", "expected_type", "expected_code"),
    [
        (404, NetworkError, KEYLESS_NETWORK),
        (500, NetworkError, KEYLESS_NETWORK),
        (429, RateLimitedError, KEYLESS_RATE_LIMITED),
        (403, RateLimitedError, KEYLESS_RATE_LIMITED),
    ],
)
def test_http_errors_become_typed_keyless_errors(
    status: int, expected_type: type, expected_code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="nope", request=request)

    with pytest.raises(expected_type) as excinfo:
        fetch_page(PAGE_URL, settings=_settings(), transport=httpx.MockTransport(handler))

    error = excinfo.value
    assert error.code == expected_code
    assert str(status) in error.message


def test_repeated_fetch_is_served_from_cache() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return html_responder(ARTICLE)(request)

    transport = httpx.MockTransport(handler)
    first = fetch_page(PAGE_URL, settings=_settings(), transport=transport)
    second = fetch_page(PAGE_URL, settings=_settings(), transport=transport)

    assert calls["count"] == 1
    assert second == first


def test_fetch_cache_can_be_disabled() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return html_responder(ARTICLE)(request)

    transport = httpx.MockTransport(handler)
    settings = _settings(KEYLESS_WEB_CACHE_TTL_FETCH="0")
    fetch_page(PAGE_URL, settings=settings, transport=transport)
    fetch_page(PAGE_URL, settings=settings, transport=transport)

    assert calls["count"] == 2


def test_connection_failure_becomes_a_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(NetworkError) as excinfo:
        fetch_page(PAGE_URL, settings=_settings(), transport=httpx.MockTransport(handler))

    assert excinfo.value.code == KEYLESS_NETWORK
    assert "ConnectError" in excinfo.value.message


# --------------------------------------------------------------------------- documents


def test_document_reports_the_status_the_text_and_the_truncation() -> None:
    document = fetch_document(
        PAGE_URL,
        settings=_settings(),
        transport=httpx.MockTransport(html_responder(ARTICLE)),
    )

    assert document.url == PAGE_URL
    assert document.status_code == 200
    assert "缓存击穿" in document.text
    assert document.truncated is False


def test_document_flags_truncation() -> None:
    document = fetch_document(
        PAGE_URL,
        max_chars=120,
        settings=_settings(),
        transport=httpx.MockTransport(html_responder(ARTICLE)),
    )

    assert document.truncated is True
    assert document.text.endswith("[truncated at 120 chars]")


@pytest.mark.parametrize("status", [404, 429, 500])
def test_document_returns_an_error_status_instead_of_raising(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="<html><body>nope</body></html>", request=request)

    document = fetch_document(
        PAGE_URL,
        allow_error_status=True,
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )

    assert document.status_code == status
    assert "nope" in document.text


def test_document_never_caches_an_error_status() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(404, text="missing", request=request)

    transport = httpx.MockTransport(handler)
    first = fetch_document(
        PAGE_URL, allow_error_status=True, settings=_settings(), transport=transport
    )
    second = fetch_document(
        PAGE_URL, allow_error_status=True, settings=_settings(), transport=transport
    )

    assert calls["count"] == 2
    assert (first.status_code, second.status_code) == (404, 404)


def test_document_still_raises_when_the_host_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(NetworkError):
        fetch_document(
            PAGE_URL,
            allow_error_status=True,
            settings=_settings(),
            transport=httpx.MockTransport(handler),
        )


def test_document_rejects_a_non_http_url() -> None:
    with pytest.raises(BadUrlError):
        fetch_document("file:///C:/Windows/win.ini", settings=_settings())
