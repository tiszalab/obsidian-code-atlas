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
LEGACY_LAUNCHD_GLOBS = ("com.obsidian-code-atlas.*.plist", "com.ghpuller.*.plist")
# cron runs jobs with a minimal PATH (typically /usr/bin:/bin), which does not
# include Homebrew's install prefixes. Prepend them (ahead of the caller's
# PATH, mirroring refresh.sh) so tools like `gh` can still be found.
CRON_PATH_PREFIXES = ("/opt/homebrew/bin", "/usr/local/bin")
CRON_PATH_SUFFIXES = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
_BENIGN_BOOTOUT_ERRORS = ("no such process", "could not find", "not loaded", "does not exist")


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


def cron_path_value(environment: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environment is None else environment
    current = env.get("PATH", "")
    segments = list(CRON_PATH_PREFIXES)
    if current:
        segments.append(current)
    segments.extend(CRON_PATH_SUFFIXES)
    return ":".join(segments)


def _escape_cron_percent(text: str) -> str:
    # A literal, unescaped "%" in a crontab command field is turned into a
    # newline by cron, truncating the command. Escape it so paths like
    # "/Users/me/100% Notes" do not silently break the job.
    return text.replace("%", "\\%")


def cron_line(output: Path, config: Optional[Path] = None, executable: Optional[str] = None,
              environment: Optional[Mapping[str, str]] = None) -> str:
    command = "PATH={} {}".format(shlex.quote(cron_path_value(environment)),
                                   shlex.join(scheduled_arguments(output, config, executable)))
    log_path = shlex.quote(str(output.resolve() / "refresh.log"))
    line = "{} {} >> {} 2>&1 {}".format(SCHEDULE, command, log_path, cron_marker(output))
    return _escape_cron_percent(line)


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


def _legacy_refresh_matches(line: str) -> bool:
    # The pre-CLI scheduler (setup.sh) points at "<checkout>/refresh.sh", not
    # at anything under the current --output vault, so matching legacy
    # entries has to key off the marker/refresh.sh reference alone rather
    # than the output path.
    return "refresh.sh" in line


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
            if _legacy_refresh_matches(line):
                index += 1
                continue
            if line.strip() in LEGACY_MARKERS and index + 1 < len(lines) and \
                    _legacy_refresh_matches(lines[index + 1]):
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


def install_cron(output: Path, config: Optional[Path] = None, executable: Optional[str] = None,
                 environment: Optional[Mapping[str, str]] = None) -> str:
    _ensure_output(output)
    current = read_crontab()
    stripped = strip_cron_entry(current, output)
    if stripped and not stripped.endswith("\n"):
        stripped += "\n"
    line = cron_line(output, config, executable, environment)
    updated = stripped + line + "\n"
    if updated != current:
        write_crontab(updated)
    return line


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


def _launchd_domain() -> str:
    return "gui/{}".format(os.getuid())


def _is_legacy_launchd_plist(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return False
    arguments = payload.get("ProgramArguments") or []
    # The checkout-bound scheduler (setup.sh) runs "refresh.sh"; anything
    # else sharing our label prefixes is one of our own installs.
    return any("refresh.sh" in str(argument) for argument in arguments)


def _legacy_launchd_paths(home: Path, current: Path) -> List[Path]:
    agents_dir = home / "Library" / "LaunchAgents"
    if not agents_dir.is_dir():
        return []
    found: List[Path] = []
    for pattern in LEGACY_LAUNCHD_GLOBS:
        for path in sorted(agents_dir.glob(pattern)):
            if path == current or not path.is_file():
                continue
            if _is_legacy_launchd_plist(path):
                found.append(path)
    return found


def _remove_launchd_plist(path: Path, best_effort: bool) -> None:
    if platform.system() == "Darwin":
        domain = _launchd_domain()
        try:
            result = _run(["launchctl", "bootout", domain, str(path)], capture_output=True, text=True)
        except OSError as exc:
            if not best_effort:
                raise SchedulerError("cannot unload LaunchAgent {}: {}".format(path, exc))
            result = None
        if not best_effort and result is not None and result.returncode != 0:
            error = (result.stderr or "").strip()
            if not any(marker in error.lower() for marker in _BENIGN_BOOTOUT_ERRORS):
                # Leave the plist in place rather than unlinking a job that
                # is still loaded: deleting it here would orphan a running
                # LaunchAgent that `scheduler` can no longer find or remove.
                raise SchedulerError("cannot unload LaunchAgent {}: {}".format(
                    path, error or "launchctl exited {}".format(result.returncode)))
    try:
        path.unlink()
    except OSError as exc:
        if not best_effort:
            raise SchedulerError("cannot remove LaunchAgent {}: {}".format(path, exc))


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
    domain = _launchd_domain()
    try:
        _run(["launchctl", "bootout", domain, str(path)], capture_output=True, text=True)
        result = _run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
    except OSError as exc:
        raise SchedulerError("cannot load LaunchAgent {}: {}".format(path, exc))
    if result.returncode != 0:
        # Don't leave a plist behind that `scheduler status` would report as
        # installed even though it never loaded.
        path.unlink(missing_ok=True)
        raise SchedulerError("cannot load LaunchAgent {}: {}".format(
            path, result.stderr.strip() or "launchctl exited {}".format(result.returncode)))
    for legacy in _legacy_launchd_paths(home, path):
        _remove_launchd_plist(legacy, best_effort=True)
    return path


def uninstall_launchd(output: Path, home: Path) -> bool:
    path = launchd_path(output, home)
    removed = False
    if path.is_file():
        _remove_launchd_plist(path, best_effort=False)
        removed = True
    for legacy in _legacy_launchd_paths(home, path):
        _remove_launchd_plist(legacy, best_effort=True)
        removed = True
    return removed


def find_cron_entry(contents: str, output: Path) -> Optional[str]:
    lines = contents.splitlines()
    marker = cron_marker(output)
    for index, line in enumerate(lines):
        if _has_marker(line, marker) or (any(_has_marker(line, legacy) for legacy in LEGACY_MARKERS) and
                                        _legacy_refresh_matches(line)):
            return line
        if line.strip() in LEGACY_MARKERS and index + 1 < len(lines) and \
                _legacy_refresh_matches(lines[index + 1]):
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
