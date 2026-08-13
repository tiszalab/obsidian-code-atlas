#!/bin/bash
# Install, update, or remove the Obsidian Code Atlas scheduler.
# Supports macOS launchd and Unix cron.
set -euo pipefail

PYTHON="${OBSIDIAN_CODE_ATLAS_PYTHON:-${GH_PULLER_PYTHON:-python3}}"
# Resolve this script's directory, following symlinks, so the install directory
# is correct even when setup.sh is invoked via a symlink.
BASE="$($PYTHON -c 'import os, sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))' "$0")"
NAME="obsidian-code-atlas"
PLIST_TEMPLATE="$BASE/obsidian-code-atlas.plist.template"

# Derive the LaunchAgent label from the install path so two vaults on the same
# Mac do not silently overwrite each other's LaunchAgent.
HASH=$("$PYTHON" -c 'import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:8])' "$BASE")
LABEL="com.${NAME}.${HASH}"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
CRON_MARKER="# obsidian-code-atlas auto-refresh"
LEGACY_CRON_MARKER="# gh_puller auto-refresh"

usage() {
    cat <<EOF
Usage: $0 [--launchd | --cron | --uninstall | --help]

  --launchd    Install a macOS LaunchAgent that runs refresh.sh daily at 08:00.
  --cron       Install a cron job that runs refresh.sh daily at 08:00.
  --uninstall  Remove both the LaunchAgent and cron job installed by this tool.
  --help       Show this message.

The install directory is detected from this script's location, so the
scheduler works wherever you put Obsidian Code Atlas.
EOF
}

install_launchd() {
    if [[ "$OSTYPE" != darwin* ]]; then
        echo "launchd is only available on macOS. Use --cron on this system." >&2
        exit 1
    fi
    if [[ ! -f "$PLIST_TEMPLATE" ]]; then
        echo "Missing $PLIST_TEMPLATE" >&2
        exit 1
    fi

    mkdir -p "$HOME/Library/LaunchAgents"
    "$PYTHON" - "$BASE" "$LABEL" "$PLIST_TEMPLATE" "$PLIST_PATH" <<'PY'
import pathlib
import sys
from xml.sax.saxutils import escape

base, label, template_path, out_path = sys.argv[1:5]
tmpl = pathlib.Path(template_path).read_text()
out = tmpl.replace("@@BASE@@", escape(base)).replace("@@LABEL@@", escape(label))
pathlib.Path(out_path).write_text(out)
PY
    chmod 644 "$PLIST_PATH"

    launchctl unload -w "$PLIST_PATH" 2>/dev/null || true
    for legacy_plist_path in "$HOME"/Library/LaunchAgents/com.ghpuller.gh_puller.*.plist; do
        if [[ -f "$legacy_plist_path" ]]; then
            launchctl unload -w "$legacy_plist_path" 2>/dev/null || true
            rm -f "$legacy_plist_path"
        fi
    done
    launchctl load -w "$PLIST_PATH"
    echo "Installed LaunchAgent: $PLIST_PATH"
    echo "  Runs daily at 08:00 and whenever you log in."
}

uninstall_launchd() {
    if [[ "$OSTYPE" != darwin* ]]; then
        return 0
    fi
    for plist_path in "$PLIST_PATH" "$HOME"/Library/LaunchAgents/com.ghpuller.gh_puller.*.plist; do
        if [[ -f "$plist_path" ]]; then
            launchctl unload -w "$plist_path" 2>/dev/null || true
            rm -f "$plist_path"
            echo "Removed LaunchAgent: $plist_path"
        fi
    done
}

# The job is a single line ending in the marker, so one fixed-string filter
# removes it no matter where Obsidian Code Atlas is installed. Trailing "# ..." is a
# shell comment, so it does not affect the command cron runs.
CRON_LINE="0 8 * * * \"$BASE/refresh.sh\" all $CRON_MARKER"

# Print the current crontab with every Obsidian Code Atlas entry removed. Also
# drops entries written by earlier versions, which put the marker on its own
# line above an unmarked job line.
strip_cron_entries() {
    ( crontab -l 2>/dev/null || true ) \
        | awk -v marker="$CRON_MARKER" -v legacy_marker="$LEGACY_CRON_MARKER" '
            index($0, marker) || index($0, legacy_marker) {
                remove_next_refresh=1
                next
            }
            remove_next_refresh && index($0, "/refresh.sh") {
                remove_next_refresh=0
                next
            }
            { remove_next_refresh=0; print }
        ' \
        | grep -vF "$BASE/refresh.sh" \
        || true
}

install_cron() {
    local tmp
    tmp="$(mktemp)"
    strip_cron_entries > "$tmp"
    printf '%s\n' "$CRON_LINE" >> "$tmp"
    crontab "$tmp"
    rm -f "$tmp"
    echo "Added cron job:"
    echo "  $CRON_LINE"
}

uninstall_cron() {
    local tmp
    tmp="$(mktemp)"
    strip_cron_entries > "$tmp"
    crontab "$tmp"
    rm -f "$tmp"
    echo "Removed cron job"
}

case "${1:-}" in
    --launchd)
        install_launchd
        ;;
    --cron)
        install_cron
        ;;
    --uninstall)
        uninstall_launchd
        uninstall_cron
        ;;
    --help|-h)
        usage
        ;;
    *)
        usage
        exit 1
        ;;
esac
