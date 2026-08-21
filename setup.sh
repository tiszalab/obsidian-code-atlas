#!/bin/bash
# Deprecated checkout compatibility wrapper for installing, updating, or
# removing the Obsidian Code Atlas scheduler. New installations should use:
#   obsidian-code-atlas scheduler install --launchd --output PATH
# This wrapper delegates to the installed Python package and only exists to
# ease migration from older checkout-inside-vault setups.
set -euo pipefail

PYTHON="${OBSIDIAN_CODE_ATLAS_PYTHON:-${GH_PULLER_PYTHON:-python3}}"
# Resolve this script's directory, following symlinks, so the install directory
# is correct even when setup.sh is invoked via a symlink.
BASE="$($PYTHON -c 'import os, sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))' "$0")"
export PYTHONPATH="$BASE/src${PYTHONPATH:+:$PYTHONPATH}"

usage() {
    cat <<EOF
Usage: $0 [--launchd | --cron | --uninstall | --help]

  --launchd    Install a macOS LaunchAgent that refreshes "$BASE" daily at 08:00.
  --cron       Install a cron job that refreshes "$BASE" daily at 08:00.
  --uninstall  Remove the scheduler for "$BASE".
  --help       Show this message.

This script delegates to the installed obsidian-code-atlas package. The
install directory is detected from this script's location.
EOF
}

case "${1:-}" in
    --launchd)
        "$PYTHON" -m obsidian_code_atlas scheduler install --launchd --output "$BASE"
        ;;
    --cron)
        "$PYTHON" -m obsidian_code_atlas scheduler install --cron --output "$BASE"
        ;;
    --uninstall)
        "$PYTHON" -m obsidian_code_atlas scheduler uninstall --output "$BASE"
        ;;
    --help|-h)
        usage
        ;;
    *)
        usage
        exit 1
        ;;
esac
