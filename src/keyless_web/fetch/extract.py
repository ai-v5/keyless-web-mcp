"""Turn a URL into something a model can read.

Two stages, deliberately separate:

``extract_page``
    Pure HTML/JSON/text -> readable text. No network, so it is trivially
    testable and reusable for content that arrives from a cache or a test.

``fetch_page``
    Network stage: transport fallback, error translation, caching, then
    :func:`extract_page`.

``fetch_document``
    The same network stage, but it reports the usable HTTP status instead of
    raising for a non-2xx answer. A host that presents a status code to its user
    (an editor, a browser tool, a harness fetch provider) cannot get that from
    :func:`fetch_page`, which turns every 4xx/5xx into a ``KEYLESS_*`` error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from trafilatura import extract as trafilatura_extract

from .. import cache as _cache
from ..config import DEFAULT_MAX_CHARS, Settings
from ..config import settings as current_settings
from ..errors import BadUrlError, KeylessWebError, ParseError
from ..net.transport import request_with_fallback, to_keyless_error

_MAX_LINKS = 30
_REMOVE_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"}


def is_http_url(url: str) -> bool:
    """Whether ``url`` is an absolute http(s) URL with a host."""
    parsed = urlparse((url or "").strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def extract_page(
    html: str,
    url: str = "",
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    include_links: bool = False,
) -> str:
    """Extract the readable main text of an HTML document.

    Args:
        html: the raw HTML payload.
        url: document URL, used for the ``URL:`` header and relative links.
        max_chars: truncate the rendered text to this many characters.
        include_links: prepend up to 30 absolute links found in the document.

    Returns:
        Text beginning with ``Title:``/``URL:`` when available.
    """
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""
    body = trafilatura_extract(
        html,
        include_comments=False,
        output_format="txt",
        url=url or None,
    )
    if not body:
        body = _fallback_text(soup)
    body = (body or "").strip()

    parts: list[str] = []
    if title:
        parts.append(f"Title: {title}")
    if url:
        parts.append(f"URL: {url}")
    if include_links:
        links = _extract_links(soup, url)
        if links:
            parts.append("Links:")
            parts.extend(f"- {link}" for link in links)
    if body:
        parts.append(body)
    return _truncate("\n\n".join(parts), max_chars)


def parse_json_payload(response: httpx.Response, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Pretty-print a JSON response, falling back to raw text."""
    return _truncate(_json_text(response), max_chars)


def _json_text(response: httpx.Response) -> str:
    """Pretty-print a JSON response body, or return its raw text when it is not JSON."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()
    try:
        return json.dumps(payload, ensure_ascii=False, indent=1)
    except (TypeError, ValueError):
        return response.text


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    """One retrieval: where it ended up, what it answered, and what it says.

    ``status_code`` is the real HTTP status, so a caller that shows it to a user
    can tell a 404 apart from a 200. ``text`` is the readable form (the same
    extraction :func:`fetch_page` returns). ``truncated`` reports whether
    ``max_chars`` cut the text.
    """

    url: str
    status_code: int
    text: str
    truncated: bool


def fetch_page(
    url: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    include_links: bool = False,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Fetch ``url`` and return readable text (or pretty-printed JSON).

    Raises:
        BadUrlError: ``url`` is not an absolute http(s) URL.
        NetworkError: no backend reached the host, or it answered 4xx/5xx.
        RateLimitedError: the host answered 429 or refuses scripted access.
        ParseError: the response could not be turned into text.
    """
    return fetch_document(
        url,
        max_chars=max_chars,
        include_links=include_links,
        settings=settings,
        transport=transport,
    ).text


def fetch_document(
    url: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    include_links: bool = False,
    allow_error_status: bool = False,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> FetchedDocument:
    """Fetch ``url`` and return the status, readable text and truncation flag.

    Args:
        url: absolute http(s) URL.
        max_chars: truncate the rendered text to this many characters.
        include_links: prepend up to 30 absolute links found in the document.
        allow_error_status: return a 4xx/5xx answer as a document instead of
            raising the matching :class:`~keyless_web.errors.KeylessWebError`.
            Transport failures still raise: there is no status to report.
        settings: configuration snapshot; defaults to the process settings.
        transport: optional transport override, for tests.

    Raises:
        BadUrlError: ``url`` is not an absolute http(s) URL.
        NetworkError: no backend reached the host.
        RateLimitedError: the host answered 429 or refuses scripted access
            (only with ``allow_error_status=False``).
        ParseError: the response could not be turned into text.
    """
    target = (url or "").strip()
    if not is_http_url(target):
        raise BadUrlError(
            f"only http:// and https:// URLs are supported (got {url!r})",
        )
    config = settings or current_settings()
    key = _cache.cache_key("fetch", target, max_chars, include_links)
    store = _cache.get_fetch_cache()
    cached = store.get(key) if config.fetch_cache_ttl > 0 else None
    if isinstance(cached, FetchedDocument):
        return cached

    try:
        response = request_with_fallback(
            "GET",
            target,
            settings=config,
            transport=transport,
            raise_for_status=not allow_error_status,
        )
        document = _document(response, max_chars=max_chars, include_links=include_links)
    except KeylessWebError:
        raise
    except Exception as exc:
        raise to_keyless_error(exc) from exc

    if config.fetch_cache_ttl > 0 and document.text and document.status_code < 400:
        store.set(key, document, config.fetch_cache_ttl)
    return document


def _document(response: httpx.Response, *, max_chars: int, include_links: bool) -> FetchedDocument:
    """Render one response into a :class:`FetchedDocument`."""
    full = _compose(response, include_links=include_links)
    return FetchedDocument(
        url=str(response.url),
        status_code=response.status_code,
        text=_truncate(full, max_chars),
        truncated=max_chars > 0 and len(full) > max_chars,
    )


def _compose(response: httpx.Response, *, include_links: bool) -> str:
    """Render the complete readable text of one response, before any truncation."""
    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type:
        return _json_text(response)
    head = response.text[:512].lower()
    if "html" in content_type or "<html" in head or "<!doctype html" in head:
        try:
            return extract_page(
                response.text,
                str(response.url),
                max_chars=0,
                include_links=include_links,
            )
        except Exception as exc:
            raise ParseError(f"could not extract text from {response.url}", cause=exc) from exc
    return response.text.strip()


def _extract_links(soup: BeautifulSoup, base_url: str, limit: int = _MAX_LINKS) -> list[str]:
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if href.startswith(("javascript:", "mailto:", "#", "data:", "tel:")):
            continue
        absolute = urljoin(base_url, href) if base_url else href
        if absolute not in links:
            links.append(absolute)
        if len(links) >= limit:
            break
    return links


def _fallback_text(soup: BeautifulSoup) -> str:
    for tag in soup(_REMOVE_TAGS):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text("\n", strip=True).splitlines() if line.strip()]
    return "\n".join(lines)


def _truncate(text: str, max_chars: int) -> str:
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + f"\n... [truncated at {max_chars} chars]"
    return text


__all__ = [
    "FetchedDocument",
    "extract_page",
    "fetch_document",
    "fetch_page",
    "is_http_url",
    "parse_json_payload",
]
