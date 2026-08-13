"""Scheduler management for installed Obsidian Code Atlas commands."""
from __future__ import annotations

import hashlib
import io
import os
import platform
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

SCHEDULE = "0 8 * * *"
CALENDAR_INTERVAL = {"Hour": 8, "Minute": 0}
LEGACY_MARKERS = ("# obsidian-code-atlas auto-refresh", "# gh_puller auto-refresh")


class SchedulerError(RuntimeError):
    pass


def scheduler_identifier(output: Path) -> str:
    digest = hashlib.sha256(str(output.resolve()).encode("utf-8")).hexdigest()[:12]
    return "com.obsidian-code-atlas.{}".format(digest)


def scheduled_arguments(output: Path, config: Optional[Path] = None,
                        executable: Optional[str] = None) -> List[str]:
    arguments = [str(Path(executable or sys.executable).resolve()), "-m", "obsidian_code_atlas",
                 "refresh", "all", "--output", str(output.resolve())]
    if config is not None:
        arguments.extend(["--config", str(config.resolve())])
    return arguments


def cron_marker(output: Path) -> str:
    return "# obsidian-code-atlas:{}".format(scheduler_identifier(output))


def cron_line(output: Path, config: Optional[Path] = None,
              executable: Optional[str] = None) -> str:
    command = shlex.join(scheduled_arguments(output, config, executable))
    log_path = shlex.quote(str(output.resolve() / "refresh.log"))
    return "{} {} >> {} 2>&1 {}".format(SCHEDULE, command, log_path, cron_marker(output))


def _run(arguments: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run(list(arguments), check=False, **kwargs)


def read_crontab() -> str:
    try:
        result = _run(["crontab", "-l"], capture_output=True, text=True)
    except OSError as exc:
        raise SchedulerError("cannot read user crontab: {}".format(exc))
    if result.returncode == 0:
        return result.stdout
    error = result.stderr.strip()
    if result.returncode == 1 and (not error or "no crontab" in error.lower()):
        return ""
    raise SchedulerError("cannot read user crontab: {}".format(error or
                                                                "crontab exited {}".format(result.returncode)))


def write_crontab(contents: str) -> None:
    try:
        result = _run(["crontab", "-"], input=contents, capture_output=True, text=True)
    except OSError as exc:
        raise SchedulerError("cannot update user crontab: {}".format(exc))
    if result.returncode != 0:
        raise SchedulerError("cannot update user crontab: {}".format(result.stderr.strip() or
                                                                      "crontab exited {}".format(result.returncode)))


def _legacy_refresh_matches(line: str, output: Path) -> bool:
    refresh_path = str(output.resolve() / "refresh.sh")
    return any(candidate in line for candidate in
               (refresh_path, shlex.quote(refresh_path), '"{}"'.format(refresh_path)))


def _has_marker(line: str, marker: str) -> bool:
    return line.rstrip().endswith(marker)


def strip_cron_entry(contents: str, output: Path) -> str:
    lines = contents.splitlines(keepends=True)
    marker = cron_marker(output)
    kept: List[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _has_marker(line, marker):
            index += 1
            continue
        if any(_has_marker(line, legacy) for legacy in LEGACY_MARKERS):
            if _legacy_refresh_matches(line, output):
                index += 1
                continue
            if line.strip() in LEGACY_MARKERS and index + 1 < len(lines) and \
                    _legacy_refresh_matches(lines[index + 1], output):
                index += 2
                continue
        kept.append(line)
        index += 1
    return "".join(kept)


def _ensure_output(output: Path) -> None:
    if output.exists() and not output.is_dir():
        raise SchedulerError("output path is not a directory: {}".format(output))
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SchedulerError("cannot create output directory {}: {}".format(output, exc))
    if not output.is_dir():
        raise SchedulerError("output path is not a directory: {}".format(output))


def install_cron(output: Path, config: Optional[Path] = None,
                 executable: Optional[str] = None) -> str:
    _ensure_output(output)
    current = read_crontab()
    stripped = strip_cron_entry(current, output)
    if stripped and not stripped.endswith("\n"):
        stripped += "\n"
    updated = stripped + cron_line(output, config, executable) + "\n"
    if updated != current:
        write_crontab(updated)
    return cron_line(output, config, executable)


def uninstall_cron(output: Path) -> bool:
    current = read_crontab()
    updated = strip_cron_entry(current, output)
    if updated == current:
        return False
    write_crontab(updated)
    return True


def launchd_path(output: Path, home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / "{}.plist".format(scheduler_identifier(output))


def launchd_payload(output: Path, config: Optional[Path] = None,
                    executable: Optional[str] = None,
                    environment: Optional[Mapping[str, str]] = None) -> dict:
    env = os.environ if environment is None else environment
    payload = {
        "Label": scheduler_identifier(output),
        "ProgramArguments": scheduled_arguments(output, config, executable),
        "RunAtLoad": True,
        "StartCalendarInterval": dict(CALENDAR_INTERVAL),
        "StandardOutPath": str(output.resolve() / "launchd.out.log"),
        "StandardErrorPath": str(output.resolve() / "launchd.err.log"),
    }
    if env.get("PATH"):
        payload["EnvironmentVariables"] = {"PATH": env["PATH"]}
    return payload


def install_launchd(output: Path, home: Path, config: Optional[Path] = None,
                    executable: Optional[str] = None,
                    environment: Optional[Mapping[str, str]] = None) -> Path:
    if platform.system() != "Darwin":
        raise SchedulerError("launchd is only available on macOS; use --cron on this system")
    _ensure_output(output)
    path = launchd_path(output, home)
    buffer = io.BytesIO()
    plistlib.dump(launchd_payload(output, config, executable, environment), buffer)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buffer.getvalue())
        path.chmod(0o644)
    except OSError as exc:
        raise SchedulerError("cannot write LaunchAgent {}: {}".format(path, exc))
    domain = "gui/{}".format(os.getuid())
    try:
        _run(["launchctl", "bootout", domain, str(path)], capture_output=True, text=True)
        result = _run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
    except OSError as exc:
        raise SchedulerError("cannot load LaunchAgent {}: {}".format(path, exc))
    if result.returncode != 0:
        raise SchedulerError("cannot load LaunchAgent {}: {}".format(
            path, result.stderr.strip() or "launchctl exited {}".format(result.returncode)))
    return path


def uninstall_launchd(output: Path, home: Path) -> bool:
    path = launchd_path(output, home)
    if not path.is_file():
        return False
    if platform.system() == "Darwin":
        domain = "gui/{}".format(os.getuid())
        try:
            _run(["launchctl", "bootout", domain, str(path)], capture_output=True, text=True)
        except OSError as exc:
            raise SchedulerError("cannot unload LaunchAgent {}: {}".format(path, exc))
    try:
        path.unlink()
    except OSError as exc:
        raise SchedulerError("cannot remove LaunchAgent {}: {}".format(path, exc))
    return True


def find_cron_entry(contents: str, output: Path) -> Optional[str]:
    lines = contents.splitlines()
    marker = cron_marker(output)
    for index, line in enumerate(lines):
        if _has_marker(line, marker) or (any(_has_marker(line, legacy) for legacy in LEGACY_MARKERS) and
                                        _legacy_refresh_matches(line, output)):
            return line
        if line.strip() in LEGACY_MARKERS and index + 1 < len(lines) and \
                _legacy_refresh_matches(lines[index + 1], output):
            return lines[index + 1]
    return None


def read_launchd_entry(output: Path, home: Path) -> Optional[dict]:
    path = launchd_path(output, home)
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise SchedulerError("cannot read LaunchAgent {}: {}".format(path, exc))
    if payload.get("Label") != scheduler_identifier(output):
        return None
    return payload


def scheduler_status(output: Path, home: Path) -> Tuple[Optional[dict], Optional[str]]:
    return read_launchd_entry(output, home), find_cron_entry(read_crontab(), output)
