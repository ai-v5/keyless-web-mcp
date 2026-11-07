"""Google News RSS parser (the ``google`` engine), fully offline."""

from __future__ import annotations

import httpx
import pytest

from keyless_web.config import load_settings
from keyless_web.engines import google_news, search
from keyless_web.errors import KEYLESS_PARSE, ParseError
from support import load_fixture, make_client, xml_responder

_RSS = load_fixture("google_news.rss")


def _client(seen: list[httpx.Request], body: str = _RSS) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return xml_responder(body)(request)

    return make_client(handler)


def test_engine_name_is_google() -> None:
    assert google_news.NAME == "google"


def test_parses_all_items() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = google_news.search(client, "climate")

    assert len(results) == 4
    assert [result.engine for result in results] == ["google"] * 4
    assert [result.title for result in results] == [
        "Example Wire: coastal cities adapt to rising seas",
        "Second story without a description",
        "Third story without a source element",
        "Fourth story with an HTML description",
    ]


def test_item_url_is_the_article_link_not_the_outlet_homepage() -> None:
    """``<source url>`` is the publisher homepage; ``<link>`` is the article.

    Using the homepage would collapse every story from one outlet into a single
    row during URL de-duplication.
    """
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = google_news.search(client, "climate")

    assert results[0].url == "https://news.example.com/articles/coastal-cities-adapt"
    assert results[2].url == "https://news.example.com/articles/third-story"
    assert len({result.url for result in results}) == 4


def test_source_attribute_is_mapped_to_the_optional_field() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = google_news.search(client, "climate")

    assert results[0].source == "Example Wire"
    assert results[0].date == "Mon, 02 Nov 2026 08:15:00 GMT"
    assert results[2].source is None  # <source> element missing
    assert results[1].source == "Another Outlet"


def test_missing_description_gives_empty_snippet() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = google_news.search(client, "climate")

    assert results[1].snippet == ""
    assert results[0].snippet == "Example description text for the coastal cities story."


def test_html_description_is_cleaned_to_plain_text() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = google_news.search(client, "climate")

    snippet = results[3].snippet
    assert "<" not in snippet and ">" not in snippet
    assert "&nbsp;" not in snippet
    assert snippet == "Fourth story Example Wire"


def test_max_results_truncates() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        results = google_news.search(client, "climate", max_results=2)

    assert len(results) == 2


def test_feed_without_items_returns_no_results() -> None:
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    with _client([], empty) as client:
        assert google_news.search(client, "nothing") == []


def test_malformed_xml_raises_parse_error() -> None:
    with _client([], "<rss><channel><item>") as client, pytest.raises(ParseError) as excinfo:
        google_news.search(client, "climate")

    assert excinfo.value.code == KEYLESS_PARSE
    assert excinfo.value.to_text().startswith("error[KEYLESS_PARSE]:")


def test_request_hits_the_news_rss_endpoint() -> None:
    seen: list[httpx.Request] = []
    with _client(seen) as client:
        google_news.search(client, "climate change")

    assert len(seen) == 1
    assert seen[0].url.host == "news.google.com"
    assert seen[0].url.path == "/rss/search"
    assert seen[0].url.params["q"] == "climate change"


def test_http_error_is_not_swallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="", request=request)

    with make_client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        google_news.search(client, "climate")


def test_same_outlet_items_are_not_collapsed_by_url_dedupe() -> None:
    """Regression: keying the result URL on ``<source url>`` collapsed a feed.

    Every story from one outlet shares the publisher homepage, so the
    orchestrator's URL de-duplication kept only the first one.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return xml_responder(_RSS)(request)

    results = search(
        "climate",
        engines=["google"],
        max_results=10,
        settings=load_settings({"KEYLESS_WEB_MIN_INTERVAL": "0"}),
        transport=httpx.MockTransport(handler),
    )

    assert len(results) == 4
    assert len({result.url for result in results}) == 4
    assert len([result for result in results if result.source == "Example Wire"]) == 2
    assert len(seen) == 1
