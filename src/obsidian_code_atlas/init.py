"""First-run initialization for Obsidian Code Atlas."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional, Tuple


class InitError(RuntimeError):
    """Raised when initialization cannot continue safely."""


def _stable_id(output: Path) -> str:
    """Stable identifier derived from the absolute output path."""
    digest = hashlib.sha256(str(output.resolve()).encode("utf-8")).hexdigest()[:12]
    return digest


def _gitignore_markers(identifier: str) -> Tuple[str, str]:
    begin = "# BEGIN obsidian-code-atlas: {}".format(identifier)
    end = "# END obsidian-code-atlas: {}".format(identifier)
    return begin, end


def resolve_vault_path(vault: Path) -> Path:
    """Validate that the supplied path is an existing directory."""
    if not vault.is_dir():
        raise InitError("vault path does not exist or is not a directory: {}".format(vault))
    return vault


def resolve_output_path(vault: Path, output: str) -> Path:
    """Resolve the atlas output path and verify it lives inside the vault."""
    candidate = Path(output).expanduser()
    if candidate.is_absolute():
        candidate = candidate.resolve()
    else:
        candidate = (vault / output).resolve()
    try:
        candidate.relative_to(vault.resolve())
    except ValueError:
        raise InitError("atlas output must be inside the vault: {}".format(candidate))
    return candidate


def find_obsidian_vault(start: Path) -> Optional[Path]:
    """Walk up from ``start`` looking for a directory containing ``.obsidian``."""
    current = start.resolve()
    while True:
        if (current / ".obsidian").is_dir():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def check_gh(environment: Mapping[str, str]) -> None:
    """Verify ``gh`` is installed and authenticated."""
    binary = shutil.which("gh", path=environment.get("PATH"))
    if binary is None:
        raise InitError("GitHub CLI (`gh`) is not installed or not on PATH")
    result = subprocess.run(
        [binary, "auth", "status"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise InitError("GitHub CLI (`gh`) is not authenticated; run `gh auth login`")


def git_worktree_root(start: Path, environment: Mapping[str, str]) -> Optional[Path]:
    """Return the Git worktree root containing ``start``, or ``None``."""
    binary = shutil.which("git", path=environment.get("PATH"))
    if binary is None or not start.exists():
        return None
    try:
        result = subprocess.run(
            [binary, "rev-parse", "--show-toplevel"],
            cwd=str(start),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", "replace").strip()
    if not text:
        return None
    return Path(text).resolve()


def _ignore_pattern(repo_root: Path, output: Path) -> str:
    rel = PurePosixPath(output.resolve().relative_to(repo_root.resolve()))
    return "/{}/".format(rel)


def _existing_pattern_covers(pattern: str, text: str) -> bool:
    """Check whether an exact ignore pattern already exists in ``text``."""
    bare = pattern.rstrip("/")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == pattern or stripped == bare:
            return True
    return False


def update_gitignore(repo_root: Path, output: Path, identifier: str) -> Tuple[bool, str]:
    """Idempotently add an anchored ignore block for ``output``.

    Returns ``(changed, reason)``.
    """
    gitignore = repo_root / ".gitignore"
    pattern = _ignore_pattern(repo_root, output)
    begin, end = _gitignore_markers(identifier)

    if gitignore.is_file():
        text = gitignore.read_text(encoding="utf-8")
        if _existing_pattern_covers(pattern, text):
            return False, "equivalent existing ignore rule"

        lines = text.splitlines(keepends=True)
        block_start: Optional[int] = None
        block_end: Optional[int] = None
        in_block = False
        for index, line in enumerate(lines):
            stripped = line.rstrip("\n\r")
            if stripped == begin:
                block_start = index
                in_block = True
            elif stripped == end and in_block:
                block_end = index
                break

        if block_start is not None and block_end is not None:
            block_text = "".join(lines[block_start:block_end + 1])
            if pattern in block_text:
                return False, "already managed"
            new_block = begin + "\n" + pattern + "\n" + end + "\n"
            updated = lines[:block_start] + [new_block] + lines[block_end + 1:]
            gitignore.write_text("".join(updated), encoding="utf-8")
            return True, "updated managed block"

    # Append a new managed block, preserving a trailing newline if possible.
    block = begin + "\n" + pattern + "\n" + end + "\n"
    if gitignore.is_file():
        base = text if text.endswith("\n") else text + "\n"
        gitignore.write_text(base + block, encoding="utf-8")
    else:
        gitignore.write_text(block, encoding="utf-8")
    return True, "added managed block"


def run_init(
    vault: Path,
    output_name: str,
    scheduler_type: str,
    track_generated: bool,
    config_path: Optional[Path],
    force: bool,
    no_refresh: bool,
    environment: Mapping[str, str],
) -> Tuple[Path, Path, dict, int]:
    """Prepare a vault directory for Obsidian Code Atlas.

    Returns ``(vault_path, output_path, info, refresh_return_code)``.
    """
    check_gh(environment)
    vault = resolve_vault_path(vault)

    if not force and not (vault / ".obsidian").is_dir():
        raise InitError(
            "no .obsidian directory found in {}; pass --force to initialize anyway".format(vault)
        )

    output = resolve_output_path(vault, output_name)
    if output.exists() and not output.is_dir():
        raise InitError("output path exists and is not a directory: {}".format(output))
    output.mkdir(parents=True, exist_ok=True)

    if config_path is not None and not config_path.is_file():
        raise InitError("configuration file does not exist: {}".format(config_path))

    info: dict = {
        "config": config_path,
        "repo_root": None,
        "git_action": "no parent Git repository",
        "scheduler": "none",
    }

    if not track_generated:
        repo_root = git_worktree_root(output, environment)
        if repo_root is not None:
            info["repo_root"] = repo_root
            changed, reason = update_gitignore(repo_root, output, _stable_id(output))
            if changed:
                info["git_action"] = "ignored via .gitignore ({})".format(reason)
            else:
                info["git_action"] = reason

    refresh_rc = 0
    if not no_refresh:
        from . import cli
        languages = cli.load_languages(output, str(config_path) if config_path else None, environment)
        context = cli.Context(output=output, languages=languages)
        refresh_rc = cli.refresh(context, "all")

    if scheduler_type != "none" and refresh_rc == 0:
        from . import scheduler as sched
        home = Path(environment.get("HOME") or Path.home()).expanduser().resolve()
        if scheduler_type == "launchd":
            sched.install_launchd(output, home, config_path, environment=environment)
            info["scheduler"] = "launchd"
        elif scheduler_type == "cron":
            sched.install_cron(output, config_path, environment=environment)
            info["scheduler"] = "cron"

    return vault, output, info, refresh_rc
