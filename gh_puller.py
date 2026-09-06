#!/usr/bin/env python3
"""Legacy checkout wrapper for Obsidian Code Atlas."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPOSITORY_DIR = Path(__file__).resolve().parent
_SRC = _REPOSITORY_DIR / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from obsidian_code_atlas.cli import main as package_main  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror GitHub activity into Obsidian.")
    parser.add_argument("command", choices=["all", "activity", "repos", "scripts", "issues"])
    parser.add_argument("--config", help="JSON configuration file")
    arguments = sys.argv[1:]
    parser.parse_args(arguments)
    return package_main(["refresh", *arguments, "--output", str(_REPOSITORY_DIR)])


if __name__ == "__main__":
    raise SystemExit(main())
