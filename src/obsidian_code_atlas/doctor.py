"""Diagnostic checks for Obsidian Code Atlas."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import __version__
from .init import _existing_pattern_covers, _ignore_pattern, git_worktree_root


def _is_writable(path: Path) -> bool:
    try:
        return os.access(path, os.W_OK)
    except OSError:
        return False


def run_doctor(
    output: Path,
    config_path: Optional[Path],
    environment: Mapping[str, str],
) -> int:
    """Run diagnostic checks and return 0 if refresh should work, 1 otherwise.

    Warnings that do not prevent refresh keep a zero status.
    """
    errors: list[str] = []
    warnings: list[str] = []

    print("obsidian-code-atlas {}".format(__version__))
    print("Python {}".format(".".join(map(str, sys.version_info[:3]))))
    print("Output: {}".format(output))

    if not output.exists():
        errors.append("output directory does not exist")
    elif not output.is_dir():
        errors.append("output path is not a directory")
    elif not _is_writable(output):
        errors.append("output directory is not writable")

    vault: Optional[Path] = None
    current = output.resolve()
    while True:
        if (current / ".obsidian").is_dir():
            vault = current
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    if vault:
        print("Obsidian vault: {}".format(vault))
    else:
        warnings.append("output does not appear to be inside an Obsidian vault")

    binary = shutil.which("gh", path=environment.get("PATH"))
    if binary:
        print("gh: {}".format(binary))
        result = subprocess.run(
            [binary, "auth", "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode == 0:
            print("gh auth: authenticated")
        else:
            errors.append("gh is not authenticated; run `gh auth login`")
    else:
        errors.append("gh is not installed or not on PATH")

    from . import cli

    effective = cli.select_config_path(
        output,
        str(config_path) if config_path else None,
        environment,
    )
    if effective:
        print("Config: {}".format(effective))
        try:
            with open(effective, "r", encoding="utf-8") as handle:
                json.load(handle)
            print("Config: valid JSON")
        except Exception as exc:
            errors.append("config does not parse: {}".format(exc))
    else:
        print("Config: (none, using defaults)")

    home = Path(environment.get("HOME") or Path.home()).expanduser().resolve()
    try:
        from . import scheduler as sched
        launchd, cron = sched.scheduler_status(output, home)
        if launchd:
            print("Scheduler: launchd installed")
        if cron:
            print("Scheduler: cron installed")
        if not launchd and not cron:
            warnings.append("no scheduler installed")
    except Exception as exc:
        warnings.append("could not read scheduler status: {}".format(exc))

    repo_root = git_worktree_root(output, environment)
    if repo_root:
        print("Git worktree: {}".format(repo_root))
        pattern = _ignore_pattern(repo_root, output)
        if pattern is None:
            warnings.append(
                "output is the Git worktree root, so generated files cannot be "
                "ignored by that repository"
            )
        else:
            gitignore = repo_root / ".gitignore"
            ignored = False
            if gitignore.is_file():
                # Match whole ignore lines: a substring test would accept an
                # unrelated rule ("/Code Atlas Backup/"), a commented-out one,
                # or a negation ("!/Code Atlas/") as proof the output is ignored.
                text = gitignore.read_text(encoding="utf-8")
                ignored = _existing_pattern_covers(pattern, text)
            if ignored:
                print("Git ignore: output is ignored")
            else:
                warnings.append(
                    "output is inside a Git worktree but not ignored; "
                    "run `init --gitignore` or add it to .gitignore"
                )
    else:
        print("Git worktree: (none)")

    for warning in warnings:
        print("Warning: {}".format(warning))
    for error in errors:
        print("Error: {}".format(error), file=sys.stderr)

    return 1 if errors else 0
