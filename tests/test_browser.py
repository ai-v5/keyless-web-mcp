"""Headless-browser backend: discovery, launch arguments and failure modes.

No browser is ever started here -- ``subprocess.run`` is replaced by a fake, so
the suite stays offline and fast.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from keyless_web import browser
from keyless_web.config import load_settings
from keyless_web.errors import KEYLESS_BROWSER, BrowserUnavailableError

OFFLINE = {"KEYLESS_WEB_MIN_INTERVAL": "0"}


def _settings(**extra: str):
    return load_settings({**OFFLINE, **extra})


@pytest.fixture
def fake_browser_path() -> str:
    """An executable that definitely exists on this machine."""
    return sys.executable


class _Completed:
    def __init__(self, stdout: bytes = b"<html></html>", stderr: bytes = b"", code: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = code


def test_find_browser_honours_an_explicit_path(fake_browser_path: str) -> None:
    found = browser.find_browser(_settings(KEYLESS_WEB_BROWSER=fake_browser_path))

    assert found is not None
    assert found.path == fake_browser_path


def test_find_browser_returns_none_for_a_missing_explicit_path() -> None:
    found = browser.find_browser(_settings(KEYLESS_WEB_BROWSER=str(Path("nope") / "chrome.exe")))

    assert found is None


def test_dump_dom_without_a_browser_explains_the_fix() -> None:
    with pytest.raises(BrowserUnavailableError) as excinfo:
        browser.dump_dom("https://example.com", settings=_settings(KEYLESS_WEB_BROWSER="off"))

    error = excinfo.value
    assert error.code == KEYLESS_BROWSER
    assert "KEYLESS_WEB_BROWSER" in error.hint


def test_dump_dom_launches_with_the_desktop_agent_and_proxy(
    monkeypatch: pytest.MonkeyPatch, fake_browser_path: str
) -> None:
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = list(args)
        captured["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(browser.subprocess, "run", fake_run)

    dom = browser.dump_dom(
        "https://www.google.com/search?q=x",
        settings=_settings(
            KEYLESS_WEB_BROWSER=fake_browser_path,
            MCP_WEB_PROXY="127.0.0.1:7890",
            KEYLESS_WEB_BROWSER_WAIT="4000",
        ),
    )

    args = captured["args"]
    assert isinstance(args, list)
    assert args[0] == fake_browser_path
    assert "--headless=new" in args
    assert "--dump-dom" in args
    assert "--proxy-server=http://127.0.0.1:7890" in args
    assert any(arg.startswith("--user-agent=Mozilla/") for arg in args)
    assert any(arg == "--virtual-time-budget=4000" for arg in args)
    assert args[-1] == "https://www.google.com/search?q=x"
    assert dom == "<html></html>"


def test_dump_dom_reports_a_silent_browser(
    monkeypatch: pytest.MonkeyPatch, fake_browser_path: str
) -> None:
    monkeypatch.setattr(
        browser.subprocess,
        "run",
        lambda args, **kwargs: _Completed(stdout=b"", stderr=b"boom", code=1),
    )

    with pytest.raises(BrowserUnavailableError) as excinfo:
        browser.dump_dom(
            "https://x.test", settings=_settings(KEYLESS_WEB_BROWSER=fake_browser_path)
        )

    assert "boom" in excinfo.value.message


def test_dump_dom_watches_for_a_hung_browser(
    monkeypatch: pytest.MonkeyPatch, fake_browser_path: str
) -> None:
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="chrome", timeout=1)

    monkeypatch.setattr(browser.subprocess, "run", fake_run)

    with pytest.raises(BrowserUnavailableError) as excinfo:
        browser.dump_dom(
            "https://x.test",
            settings=_settings(KEYLESS_WEB_BROWSER=fake_browser_path),
            timeout=1,
        )

    assert "KEYLESS_WEB_BROWSER_TIMEOUT" in excinfo.value.hint


def test_dump_dom_reports_an_unlaunchable_binary(
    monkeypatch: pytest.MonkeyPatch, fake_browser_path: str
) -> None:
    def fake_run(args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(browser.subprocess, "run", fake_run)

    with pytest.raises(BrowserUnavailableError) as excinfo:
        browser.dump_dom(
            "https://x.test", settings=_settings(KEYLESS_WEB_BROWSER=fake_browser_path)
        )

    assert "cannot start" in excinfo.value.message


def test_dump_dom_rejects_an_absurdly_large_dom(
    monkeypatch: pytest.MonkeyPatch, fake_browser_path: str
) -> None:
    monkeypatch.setattr(browser, "_MAX_DOM_BYTES", 8)
    monkeypatch.setattr(
        browser.subprocess,
        "run",
        lambda args, **kwargs: _Completed(stdout=b"<html>far too big</html>"),
    )

    with pytest.raises(BrowserUnavailableError) as excinfo:
        browser.dump_dom(
            "https://x.test", settings=_settings(KEYLESS_WEB_BROWSER=fake_browser_path)
        )

    assert "unexpectedly large" in excinfo.value.message


def test_find_browser_falls_back_to_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser, "_existing", lambda path: None)
    monkeypatch.setattr(browser.shutil, "which", lambda name: rf"C:\fake\{name}")

    found = browser.find_browser(_settings())

    assert found is not None
    assert found.path.endswith(("chrome.exe", "msedge.exe", "google-chrome", "chromium"))


def test_no_console_window_flag_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser.sys, "platform", "linux")

    assert browser._creation_flags() == 0
