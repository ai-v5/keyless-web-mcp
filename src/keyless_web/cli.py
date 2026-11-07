"""Command line entry point behind ``keyless-web-mcp`` and ``python -m keyless_web``."""

from __future__ import annotations

import argparse
import logging

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keyless-web-mcp",
        description=(
            "Keyless web search and page reading over the Model Context Protocol "
            "(stdio transport). Configure it through environment variables, see README."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"keyless-web-mcp {__version__}",
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=("stdio",),
        help="MCP transport to serve (only stdio is supported).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, then serve the MCP server on stdio."""
    build_parser().parse_args(argv)

    # stderr belongs to the client's log pane; httpx logs one INFO line per
    # request, which drowns out anything useful there.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Install the system certificate store before any client is built: corporate
    # TLS interception and desktop antivirus both break the bundled CA set.
    from .net.transport import inject_system_trust

    inject_system_trust()

    from .server import app

    app.run(transport="stdio")
    return 0


__all__ = ["build_parser", "main"]
