"""Headless browser backend: render a page with an installed Chrome or Edge.

Why this exists
---------------
``google.com/search`` answers a plain HTTP client with a JavaScript shell (see
:mod:`keyless_web.engines.google_news`), so the only way to read the real result
page is to render it. A browser also opens the door to any other client-rendered
source later.

What it is not
--------------
No automation library: the backend runs the installed browser once in
``--headless=new`` mode with ``--dump-dom`` and returns the serialized DOM. That
keeps the package dependency-free and lets the browser own proxy resolution, so
a corporate system proxy or PAC script works without extra configuration.

The one non-obvious detail is the user agent. Google answers the stock
``HeadlessChrome`` agent with an "unusual traffic" interstitial; with a desktop
agent the same headless browser returns normal results. The default agent is
therefore the desktop one already used by the HTTP engines.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Settings, normalize_proxy
from .config import settings as current_settings
from .errors import BrowserUnavailableError

#: Extra seconds granted on top of the virtual time budget before a launch is
#: declared hung. Chrome exits on its own when the budget elapses; this is the
#: watchdog for a browser that never does.
_TIMEOUT_SLACK = 15.0

#: A DOM larger than this is almost certainly not a result page.
_MAX_DOM_BYTES = 24 * 1024 * 1024

_WINDOWS_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

_POSIX_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)

_POSIX_NAMES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
)


@dataclass(frozen=True, slots=True)
class Browser:
    """One usable browser installation."""

    path: str

    @property
    def name(self) -> str:
        return Path(self.path).stem


def _existing(path: str) -> str | None:
    expanded = os.path.expandvars(os.path.expanduser(path))
    return expanded if os.path.isfile(expanded) else None


#: Values that explicitly switch the browser off instead of naming a path.
_OFF = {"off", "none", "0", "false", "no"}


def find_browser(settings: Settings | None = None) -> Browser | None:
    """Locate a Chrome/Edge/Chromium executable, or ``None`` when none exists.

    Order: ``Settings.browser`` (from ``KEYLESS_WEB_BROWSER``), the well-known
    install paths of this platform, then ``PATH``. An explicit ``off`` -- or a
    path that no longer exists -- disables the browser instead of silently
    falling back to autodetection, so a broken override cannot resurrect a
    browser the operator meant to switch off.
    """
    current = settings or current_settings()
    configured = (current.browser or "").strip()
    if configured.lower() in _OFF:
        return None
    if configured:
        found = _existing(configured)
        return Browser(found) if found else None

    if sys.platform == "win32":
        for candidate in _WINDOWS_CANDIDATES:
            found = _existing(candidate)
            if found:
                return Browser(found)
        for name in ("chrome.exe", "msedge.exe"):
            found = shutil.which(name)
            if found:
                return Browser(found)
        return None

    for candidate in _POSIX_CANDIDATES:
        found = _existing(candidate)
        if found:
            return Browser(found)
    for name in _POSIX_NAMES:
        found = shutil.which(name)
        if found:
            return Browser(found)
    return None


def _launch_args(
    browser: Browser,
    url: str,
    *,
    profile_dir: str,
    user_agent: str,
    wait_ms: int,
    proxy: str | None,
) -> list[str]:
    args = [
        browser.path,
        "--headless=new",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-sync",
        "--hide-scrollbars",
        "--mute-audio",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        f"--user-data-dir={profile_dir}",
        f"--user-agent={user_agent}",
        f"--virtual-time-budget={wait_ms}",
        "--dump-dom",
        url,
    ]
    if proxy:
        args.insert(1, f"--proxy-server={proxy}")
    return args


def dump_dom(
    url: str,
    *,
    settings: Settings | None = None,
    timeout: float | None = None,
) -> str:
    """Render ``url`` in a headless browser and return the final DOM as text.

    Args:
        url: absolute ``http(s)`` URL to open.
        settings: configuration snapshot; defaults to the process settings.
        timeout: wall-clock budget in seconds; defaults to
            ``Settings.browser_timeout``.

    Raises:
        BrowserUnavailableError: no browser found, the launch failed, or it was
            killed or timed out. The message names the fix.
    """
    current = settings or current_settings()
    browser = find_browser(current)
    if browser is None:
        raise BrowserUnavailableError(
            "no Chrome, Edge or Chromium executable was found",
            hint=(
                "Install Chrome or Edge, or point KEYLESS_WEB_BROWSER at the "
                "browser executable. Engines that need a browser stay unavailable "
                "until then; the HTTP engines keep working."
            ),
        )

    budget_s = timeout if timeout is not None else current.browser_timeout
    profile_dir = tempfile.mkdtemp(prefix="keyless-web-browser-")
    args = _launch_args(
        browser,
        url,
        profile_dir=profile_dir,
        user_agent=current.user_agent,
        wait_ms=current.browser_wait_ms,
        proxy=normalize_proxy(current.proxy or ""),
    )
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            timeout=budget_s + _TIMEOUT_SLACK,
            check=False,
            creationflags=_creation_flags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise BrowserUnavailableError(
            f"{browser.name} did not finish rendering {url} within "
            f"{budget_s + _TIMEOUT_SLACK:.0f}s",
            hint="Raise KEYLESS_WEB_BROWSER_TIMEOUT, or use an HTTP engine instead.",
            cause=exc,
        ) from exc
    except OSError as exc:
        raise BrowserUnavailableError(
            f"cannot start {browser.path}: {exc}",
            hint=(
                "Point KEYLESS_WEB_BROWSER at a working Chrome/Edge executable, "
                "or use an HTTP engine instead."
            ),
            cause=exc,
        ) from exc
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)

    dom = completed.stdout.decode("utf-8", errors="replace")
    if not dom.strip():
        detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        tail = detail[-1] if detail else f"exit code {completed.returncode}"
        raise BrowserUnavailableError(
            f"{browser.name} returned no DOM for {url} ({tail})",
            hint=(
                "Retry once; if it persists, check that the browser starts "
                "normally outside the harness."
            ),
        )
    if len(dom.encode("utf-8", errors="replace")) > _MAX_DOM_BYTES:
        raise BrowserUnavailableError(
            f"{browser.name} returned an unexpectedly large DOM for {url}"
        )
    return dom


def _creation_flags() -> int:
    """Hide the console window on Windows; ``0`` elsewhere."""
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


__all__ = ["Browser", "dump_dom", "find_browser"]
