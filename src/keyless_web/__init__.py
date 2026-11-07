"""Keyless web search and page reading for Model Context Protocol clients.

Importing this package is intentionally cheap: it does not pull in the MCP SDK
or configure TLS, so unit tests can import the engines without side effects.
Use :mod:`keyless_web.server` for the tool server and :mod:`keyless_web.cli`
for the ``keyless-web-mcp`` entry point.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:  # installed distribution (pip/uvx/pipx)
    __version__ = version("keyless-web-mcp")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.1.0"

__all__ = ["__version__"]
