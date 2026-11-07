"""Page fetching: network fallback plus readable-text extraction."""

from __future__ import annotations

from .extract import extract_page, fetch_page, is_http_url, parse_json_payload

__all__ = ["extract_page", "fetch_page", "is_http_url", "parse_json_payload"]
