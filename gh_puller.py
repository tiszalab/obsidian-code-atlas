#!/usr/bin/env python3
"""Legacy checkout wrapper for Obsidian Code Atlas."""
from __future__ import annotations

import sys
from pathlib import Path

_REPOSITORY_DIR = Path(__file__).resolve().parent
_SRC = _REPOSITORY_DIR / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from obsidian_code_atlas.cli import main as package_main  # noqa: E402


def main() -> int:
    section = sys.argv[1] if len(sys.argv) > 1 else "all"
    return package_main(["refresh", section, "--output", str(_REPOSITORY_DIR)])


if __name__ == "__main__":
    raise SystemExit(main())
