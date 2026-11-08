"""Rendered Google engine (``google_web``): DOM parsing, redirect recovery, wiring.

Everything here is offline: the DOM comes from a fixture and the redirect hop is
served by ``httpx.MockTransport``.
"""

from __future__ import annotations

import httpx
import pytest

from keyless_web.config import load_settings
from keyless_web.engines import google_web
from support import load_fixture, make_client

OFFLINE = {"KEYLESS_WEB_MIN_INTERVAL": "0"}

GOTO = "/goto?url=CAESYQHEIkXX1QHoqrPank3Fm_1V8dy27BhaAGNarSuq"


def _settings(**extra: str):
    return load_settings({**OFFLINE, **extra})


def _dom() -> str:
    return load_fixture("google_web_results.html")


def _resolver(mapping: dict[str, str]):
    def resolve(href: str) -> str | None:
        return mapping.get(href)

    return resolve


# --------------------------------------------------------------------------- url


def test_engine_name_is_google_web() -> None:
    assert google_web.NAME == "google_web"


def test_build_url_encodes_query_and_result_count() -> None:
    url = google_web.build_url("python asyncio & more", num=7)

    assert url.startswith("https://www.google.com/search?")
    assert "q=python+asyncio+%26+more" in url
    assert "num=7" in url
    assert "pws=0" in url


# --------------------------------------------------------------------------- parsing


def test_parses_only_organic_blocks() -> None:
    results = google_web.parse_results(_dom(), resolve=_resolver({}))

    assert [result.title for result in results] == [
        "Alpha Python Guide",
        "Beta result",
        "Gamma unresolved",
    ]
    assert [result.engine for result in results] == ["google_web"] * 3
    assert all("Sponsored" not in result.title for result in results)
    assert all("AI Mode" not in result.title for result in results)


def test_resolver_supplies_goto_targets() -> None:
    results = google_web.parse_results(
        _dom(),
        resolve=_resolver({GOTO: "https://example.com/alpha"}),
    )

    assert results[0].url == "https://example.com/alpha"
    assert results[0].snippet.startswith("Alpha snippet")


def test_direct_http_href_is_used_without_resolving() -> None:
    seen: list[str] = []

    def resolve(href: str) -> str | None:
        seen.append(href)
        return None

    results = google_web.parse_results(_dom(), resolve=resolve)

    assert results[1].url == "https://example.org/beta"
    assert "https://example.org/beta" not in seen


def test_cite_is_the_fallback_when_the_redirect_fails() -> None:
    results = google_web.parse_results(_dom(), resolve=_resolver({}))

    assert results[0].url == "https://example.com/alpha"
    assert results[2].url == "https://gamma.example/docs/intro"


def test_max_results_truncates() -> None:
    results = google_web.parse_results(_dom(), resolve=_resolver({}), max_results=2)

    assert len(results) == 2


def test_page_without_results_is_empty() -> None:
    assert google_web.parse_results("<html><body>no results</body></html>") == []


# --------------------------------------------------------------------------- edge cases

_ONE_BLOCK = """<div class="MjjYud"><div class="g">
  <a href="{href}"><h3>{title}</h3></a>{cite}{snippet}
</div></div>"""


def _block(
    href: str,
    title: str = "Title",
    cite: str = "",
    snippet: str = ' <div class="VwiC3b">snip</div>',
) -> str:
    cite_html = f"<div><cite>{cite}</cite></div>" if cite else ""
    return _ONE_BLOCK.format(href=href, title=title, cite=cite_html, snippet=snippet)


def test_legacy_url_redirect_is_parsed_without_a_request() -> None:
    dom = _block("/url?q=https://example.net/docs&amp;sa=U")

    results = google_web.parse_results(dom)

    assert [result.url for result in results] == ["https://example.net/docs"]


def test_ad_network_targets_are_dropped() -> None:
    dom = _block("https://www.googlesyndication.com/pagead/click")

    assert google_web.parse_results(dom) == []


def test_cite_without_a_scheme_gets_https() -> None:
    dom = _block("/goto?url=OPAQUE", cite="docs.example.com › guide")

    results = google_web.parse_results(dom)

    assert [result.url for result in results] == ["https://docs.example.com/guide"]


def test_unresolvable_link_without_a_cite_is_dropped() -> None:
    assert google_web.parse_results(_block("/goto?url=OPAQUE")) == []


def test_blank_title_is_skipped() -> None:
    assert google_web.parse_results(_block("https://example.com/x", title="   ")) == []


def test_missing_snippet_container_yields_an_empty_snippet() -> None:
    results = google_web.parse_results(
        _block("https://example.com/x", snippet=""),
    )

    assert results[0].snippet == ""


def test_redirect_without_a_location_header_resolves_to_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="no redirect here", request=request)

    with make_client(handler) as client:
        assert google_web._resolve_redirect(client, "/goto?url=OPAQUE") is None


# --------------------------------------------------------------------------- wiring


def test_search_renders_the_page_then_resolves_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered: list[str] = []

    def fake_dump_dom(url: str, *, settings: object = None) -> str:
        rendered.append(url)
        return _dom()

    monkeypatch.setattr(google_web._browser, "dump_dom", fake_dump_dom)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://example.com/alpha"},
            request=request,
        )

    with make_client(handler) as client:
        results = google_web.search(client, "python", 3, settings=_settings())

    assert rendered and "q=python" in rendered[0]
    assert results[0].url == "https://example.com/alpha"
    assert results[1].url == "https://example.org/beta"


def test_search_survives_a_dead_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_web._browser,
        "dump_dom",
        lambda url, *, settings=None: _dom(),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with make_client(handler) as client:
        results = google_web.search(client, "python", 3, settings=_settings())

    # Both redirector links fall back to their <cite> breadcrumb.
    assert [result.url for result in results] == [
        "https://example.com/alpha",
        "https://example.org/beta",
        "https://gamma.example/docs/intro",
    ]
