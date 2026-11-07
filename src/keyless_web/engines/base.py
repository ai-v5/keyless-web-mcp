"""Engine abstraction: one result shape, one call convention.

Every engine is a module inside :mod:`keyless_web.engines` exposing a module
level ``search(client, query, max_results)`` function. An engine receives an
already-configured :class:`httpx.Client` and returns :class:`SearchResult`
objects. That keeps engines pure enough to test against
``httpx.MockTransport`` without any network access, and it keeps retry, proxy,
rate limiting and caching policy out of the engines entirely.
"""

from __future__ import annotations

import html as html_mod
import re
from typing import Protocol, runtime_checkable

import httpx
from pydantic import BaseModel

_TAG_RE = re.compile(r"<[^>]+>")


def clean_snippet(text: str | None) -> str:
    """Strip HTML tags, unescape entities and collapse whitespace."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", html_mod.unescape(_TAG_RE.sub("", text))).strip()


class SearchResult(BaseModel):
    """One search hit, engine-agnostic.

    ``to_dict()`` drops unset optional fields, which keeps the JSON handed to
    the model small: only news engines normally fill ``source``/``date``.
    """

    title: str
    url: str
    snippet: str = ""
    engine: str
    source: str | None = None
    date: str | None = None

    def to_dict(self) -> dict[str, str]:
        """JSON-ready mapping with ``None`` fields omitted."""
        data = self.model_dump(mode="json")
        return {key: value for key, value in data.items() if value is not None}


@runtime_checkable
class Engine(Protocol):
    """Structural interface every engine module satisfies."""

    name: str

    def search(
        self,
        client: httpx.Client,
        query: str,
        max_results: int = 10,
    ) -> list[SearchResult]: ...


__all__ = ["Engine", "SearchResult", "clean_snippet"]
