#!/bin/bash
# Refresh the gh_puller Obsidian dashboard. Safe to run from cron/launchd,
# where PATH is minimal — so we add common install locations and resolve the
# install directory from this script's location.
set -euo pipefail

# gh is commonly installed via Homebrew (Apple Silicon or Intel) or at the
# system level. Add those locations before the existing PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Resolve this script's directory so the wrapper works wherever it is installed.
BASE="$(cd "$(dirname "$0")" && pwd)"

# Use the python3 found on PATH, or let the user override with an env variable.
PYTHON="${GH_PULLER_PYTHON:-python3}"
LOG="$BASE/refresh.log"

cd "$BASE"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') refresh start ====="
  "$PYTHON" "$BASE/gh_puller.py" "${1:-all}"
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') refresh done ====="
} >> "$LOG" 2>&1
