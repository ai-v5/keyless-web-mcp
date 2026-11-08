"""Google web search through a headless browser (the ``google_web`` engine).

``google.com/search`` no longer serves parseable HTML to a plain HTTP client --
it answers with a JavaScript shell (see :mod:`keyless_web.engines.google_news`
for the RSS workaround). Rendering it in a real browser restores the web index:
``--dump-dom`` returns the finished result page.

Two details decide whether this engine works at all:

* **The user agent.** Google answers the stock ``HeadlessChrome`` agent with an
  "unusual traffic" interstitial. With a desktop agent the same headless browser
  returns normal results. :mod:`keyless_web.browser` already defaults to one.
* **The result URLs.** Google wraps outbound links in ``/goto?url=<opaque>``
  where the payload is not URL-encoded but an encrypted protobuf. Two fallbacks
  recover the real target: the ``302`` that ``/goto`` itself answers (exact, no
  cookies needed), and the ``<cite>`` line printed under every title
  (approximate, used only when the redirect cannot be read).

Only ``div.MjjYud`` blocks carrying an ``h3`` title are kept; that drops the
AI-mode card, the ad slots and the "related searches" strip.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .. import browser as _browser
from ..config import Settings
from ..config import settings as current_settings
from .base import SearchResult, clean_snippet

NAME = "google_web"
ENDPOINT = "https://www.google.com/search"

#: Path prefixes of Google's own redirector links.
_GOTO_PATH = "/goto"
_URL_PATH = "/url"

#: Ad hosts that must never reach the model as a result.
_REJECTED_HOSTS = ("googleadservices.com", "googlesyndication.com")

#: Upper bound on concurrent redirect resolutions for one search.
_MAX_RESOLVERS = 8

#: What :meth:`parse_results` calls to turn a redirect link into its target.
Resolver = Callable[[str], "str | None"]


def build_url(query: str, *, num: int = 10, lang: str = "en") -> str:
    """Return the search URL for ``query`` (personalization off)."""
    return f"{ENDPOINT}?q={quote_plus(query)}&hl={lang}&num={max(1, num)}&pws=0"


def search(
    client: httpx.Client,
    query: str,
    max_results: int = 10,
    *,
    settings: Settings | None = None,
) -> list[SearchResult]:
    """Render a Google result page and return at most ``max_results`` hits."""
    config = settings or current_settings()
    dom = _browser.dump_dom(build_url(query, num=max_results), settings=config)
    return parse_results(
        dom,
        resolve=lambda href: _resolve_redirect(client, href),
        max_results=max_results,
    )


def parse_results(
    dom: str,
    *,
    resolve: Resolver | None = None,
    max_results: int = 10,
) -> list[SearchResult]:
    """Turn a rendered Google result page into :class:`SearchResult` objects.

    Args:
        dom: the rendered HTML.
        resolve: ``resolve(href) -> str | None`` turning a ``/goto?url=...``
            link into its real target. ``None`` skips resolution and leaves the
            ``<cite>`` fallback in charge.
        max_results: upper bound on returned hits.

    Pure apart from ``resolve``; the offline suite drives it against a fixture.
    """
    soup = BeautifulSoup(dom, "html.parser")
    pending: list[tuple[Any, str, str, str, str | None]] = []
    for block in soup.select("div.MjjYud") or soup.select("div.g"):
        heading = block.find("h3")
        if heading is None:
            continue
        anchor = heading.find_parent("a") or block.select_one("a[href]")
        href = str(anchor.get("href") or "") if anchor is not None else ""
        if not _is_result_link(href):
            continue
        title = _text(heading)
        if not title:
            continue
        pending.append((block, title, _snippet(block), href, _direct_target(href)))
        if len(pending) >= max_results:
            break

    resolved = _resolve_many(
        [href for _block, _title, _snip, href, direct in pending if direct is None],
        resolve,
    )

    results: list[SearchResult] = []
    for block, title, snippet, href, direct in pending:
        target = direct or resolved.get(href) or _cite_target(block)
        if not target or any(host in target for host in _REJECTED_HOSTS):
            continue
        results.append(
            SearchResult(
                title=title,
                url=target,
                snippet=clean_snippet(snippet),
                engine=NAME,
            )
        )
        if len(results) >= max_results:
            break
    return results


def _text(node: Any) -> str:
    """Normalized text of a soup node: source spacing kept, runs collapsed."""
    raw = node.get_text("", strip=False) if hasattr(node, "get_text") else str(node)
    return " ".join(str(raw).split())


def _snippet(block: Any) -> str:
    for selector in ("div.VwiC3b", "div.IsZvec", "span.aCOpRe"):
        found = block.select_one(selector)
        if found is not None:
            return _text(found)
    return ""


def _is_result_link(href: str) -> bool:
    """Whether ``href`` can point at an organic result."""
    if not href:
        return False
    if "/aclk" in href or "/pagead/" in href or "googleadservices" in href:
        return False
    # Google's own navigation: refinements, AI mode, related queries.
    if href.startswith("/search?") or href.startswith("/?") or "/search?" in href:
        return False
    return href.startswith("http") or href.startswith(_GOTO_PATH) or href.startswith(_URL_PATH)


def _direct_target(href: str) -> str | None:
    """The real URL when ``href`` already carries it, else ``None``."""
    if href.startswith(_URL_PATH + "?") or "/url?q=" in href:
        query = parse_qs(urlparse(href).query)
        candidates = query.get("q") or query.get("url") or []
        target = candidates[0] if candidates else ""
        return target if target.startswith("http") else None
    if href.startswith("http") and "google.com" not in urlparse(href).netloc:
        return href
    return None


def _resolve_many(hrefs: list[str], resolve: Resolver | None) -> dict[str, str]:
    """Resolve redirect links in parallel, keeping only successes."""
    if not hrefs or resolve is None:
        return {}
    unique = list(dict.fromkeys(hrefs))
    workers = max(1, min(_MAX_RESOLVERS, len(unique)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        targets = list(pool.map(resolve, unique))
    return {href: target for href, target in zip(unique, targets, strict=True) if target}


def _resolve_redirect(client: httpx.Client, href: str) -> str | None:
    """Follow a ``/goto?url=...`` link one hop and return its target."""
    absolute = href if href.startswith("http") else urljoin(f"https://{_HOST}", href)
    try:
        response = client.get(absolute, follow_redirects=False)
    except httpx.HTTPError:
        return None
    location = response.headers.get("location") or ""
    if not location:
        return None
    target = urljoin(absolute, location)
    return target if target.startswith("http") else None


def _cite_target(block: Any) -> str | None:
    """Rebuild the target URL from the ``<cite>`` line under a title.

    Google prints ``https://www.deepseek.com › harness``; the breadcrumb
    separator is the only structural hint, so the result is approximate -- good
    enough to cite, never used when the redirect resolved.
    """
    if block is None:
        return None
    cite = block.select_one("cite")
    if cite is None:
        return None
    parts = [part.strip() for part in _text(cite).split("›")]
    if not parts or not parts[0]:
        return None
    head = parts[0]
    if not head.startswith("http"):
        head = f"https://{head}"
    tail = "/".join(part.strip().strip("/") for part in parts[1:] if part.strip())
    return f"{head.rstrip('/')}/{tail}" if tail else head


_HOST = urlparse(ENDPOINT).netloc

__all__ = ["ENDPOINT", "NAME", "build_url", "parse_results", "search"]
