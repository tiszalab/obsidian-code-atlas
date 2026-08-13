#!/bin/bash
# Refresh the Obsidian Code Atlas dashboard. Safe to run from cron/launchd,
# where PATH is minimal — so we add common install locations and resolve the
# install directory from this script's location.
set -euo pipefail

# gh is commonly installed via Homebrew (Apple Silicon or Intel) or at the
# system level. Add those locations around the existing PATH so the caller's
# preferred Python (e.g. pyenv/conda) stays ahead of the system interpreter,
# while still having a fallback for minimal cron/launchd environments.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}:/usr/bin:/bin:/usr/sbin:/sbin"

# Use the python3 found on PATH, or let the user override with an env variable.
PYTHON="${OBSIDIAN_CODE_ATLAS_PYTHON:-${GH_PULLER_PYTHON:-python3}}"

# Resolve this script's directory, following symlinks, so the wrapper works
# wherever it is installed and even if it is invoked via a symlink.
BASE="$(cd -- "$( dirname -- "$($PYTHON -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$0" )" )" && pwd )"
LOG="$BASE/refresh.log"

cd "$BASE"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') refresh start ====="
  "$PYTHON" "$BASE/gh_puller.py" "${1:-all}"
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') refresh done ====="
} >> "$LOG" 2>&1
