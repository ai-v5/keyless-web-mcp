"""Page fetching: network fallback plus readable-text extraction."""

from __future__ import annotations

from .extract import (
    FetchedDocument,
    extract_page,
    fetch_document,
    fetch_page,
    is_http_url,
    parse_json_payload,
)

__all__ = [
    "FetchedDocument",
    "extract_page",
    "fetch_document",
    "fetch_page",
    "is_http_url",
    "parse_json_payload",
]
