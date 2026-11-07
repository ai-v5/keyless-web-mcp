"""``python -m keyless_web`` -- same behaviour as the ``keyless-web-mcp`` script."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
