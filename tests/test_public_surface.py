"""Package contract: cheap import, ``__version__``, and the ``python -m`` entry point.

The import-side-effect checks run in a *fresh interpreter* on purpose: that is
the only way to prove ``import keyless_web`` does not drag in the MCP SDK or the
network stack.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import keyless_web
from support import REPO_ROOT, SRC_DIR

PYPROJECT = REPO_ROOT / "pyproject.toml"


def _run(args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONHOME", None)
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


# --------------------------------------------------------------------------- version


def test_version_is_a_release_string() -> None:
    assert isinstance(keyless_web.__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[.\-+][0-9A-Za-z.\-+]*)?", keyless_web.__version__)


def test_version_matches_pyproject() -> None:
    tomllib = pytest.importorskip("tomllib")
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]

    assert keyless_web.__version__ == declared


def test_public_all_is_minimal() -> None:
    assert keyless_web.__all__ == ["__version__"]


# --------------------------------------------------------------------------- import side effects


def test_import_does_not_pull_in_the_mcp_sdk_or_the_server() -> None:
    program = (
        "import sys, json, keyless_web;"
        "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'mcp', 'httpx', 'trafilatura', 'bs4'})))"
    )

    completed = _run(["-c", program])

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip()) == []


def test_import_does_not_import_any_submodule() -> None:
    """A bare ``import keyless_web`` must not pull in server/fetch/engines.

    Checked in a fresh interpreter: by the time this module runs, other test
    modules have already imported those submodules in-process.
    """
    program = (
        "import sys, json, keyless_web;"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('keyless_web.'))))"
    )

    completed = _run(["-c", program])

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip()) == []


def test_import_has_no_output_or_side_effects() -> None:
    completed = _run(["-c", "import keyless_web"])

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""


# --------------------------------------------------------------------------- CLI


def test_module_entry_point_help_works() -> None:
    completed = _run(["-m", "keyless_web", "--help"])

    assert completed.returncode == 0, completed.stderr
    output = (completed.stdout + completed.stderr).lower()
    assert "usage" in output
    assert "keyless-web-mcp" in output


def test_module_entry_point_reports_the_version() -> None:
    completed = _run(["-m", "keyless_web", "--version"])

    assert completed.returncode == 0, completed.stderr
    assert keyless_web.__version__ in completed.stdout


def test_module_entry_point_rejects_unknown_transports() -> None:
    completed = _run(["-m", "keyless_web", "--transport", "http"])

    assert completed.returncode != 0
    assert "invalid choice" in (completed.stderr + completed.stdout).lower()


def test_cli_builds_the_same_parser_as_the_module_entry_point() -> None:
    from keyless_web.cli import build_parser

    parser = build_parser()
    actions = {action.dest for action in parser._actions}

    assert "help" in actions
    assert "transport" in actions


# --------------------------------------------------------------------------- packaging surface


def test_source_tree_layout_is_what_the_package_expects() -> None:
    package = SRC_DIR / "keyless_web"

    for relative in (
        "__init__.py",
        "__main__.py",
        "cli.py",
        "config.py",
        "cache.py",
        "errors.py",
        "server.py",
        "worker.py",
        "engines/base.py",
        "engines/__init__.py",
        "net/transport.py",
        "fetch/extract.py",
    ):
        assert (package / relative).is_file(), f"missing module: {relative}"


def test_no_test_module_is_shadowed_by_the_package() -> None:
    """A stray ``tests/__init__.py`` would break the flat ``from support import``."""
    assert not (Path(__file__).resolve().parent / "__init__.py").exists()
