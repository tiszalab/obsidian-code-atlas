#!/bin/bash
# Refresh the gh_puller Obsidian dashboard. Safe to run from cron/launchd,
# where PATH is minimal — so we set it explicitly and use absolute paths.
set -euo pipefail

# gh lives in Homebrew; add it (and common bins) to PATH for cron.
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# The tool and all its output live in this one folder.
BASE="/Users/michaeltisza/mike_tisza/github_repos/TiszaMike_notes/gh_puller"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
LOG="$BASE/refresh.log"

cd "$BASE"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') refresh start ====="
  "$PYTHON" "$BASE/gh_puller.py" "${1:-all}"
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') refresh done ====="
} >> "$LOG" 2>&1
