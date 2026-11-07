"""Optional real-network smoke tests.

Skipped by default: the offline suite must never depend on the internet. Run them
deliberately with ``pytest -m net --run-net`` when you want to know whether the
upstream endpoints still work.
"""

from __future__ import annotations

import httpx
import pytest

from keyless_web.config import load_settings
from keyless_web.engines import duckduckgo
from keyless_web.fetch import fetch_page

pytestmark = pytest.mark.net


def test_duckduckgo_still_answers_with_results() -> None:
    with httpx.Client(follow_redirects=True, timeout=30.0) as client:
        results = duckduckgo.search(client, "python asyncio", 3)

    assert results, "DuckDuckGo returned no results (markup change or blocked)"
    assert all(result.url.startswith(("http://", "https://")) for result in results)
    assert all(result.engine == "duckduckgo" for result in results)


def test_fetch_page_reads_a_real_page() -> None:
    text = fetch_page(
        "https://example.com/",
        settings=load_settings({"KEYLESS_WEB_MIN_INTERVAL": "0"}),
    )

    assert "Example Domain" in text
    assert text.startswith("Title: ")
