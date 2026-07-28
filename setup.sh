#!/bin/bash
# Install, update, or remove the gh_puller scheduler.
# Supports macOS launchd and Unix cron.
set -euo pipefail

BASE="$(cd "$(dirname "$0")" && pwd)"
NAME="gh_puller"
LABEL="com.ghpuller.${NAME}"
PLIST_TEMPLATE="$BASE/gh_puller.plist.template"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
CRON_MARKER="# gh_puller auto-refresh"

usage() {
    cat <<EOF
Usage: $0 [--launchd | --cron | --uninstall | --help]

  --launchd    Install a macOS LaunchAgent that runs refresh.sh daily at 08:00.
  --cron       Install a cron job that runs refresh.sh daily at 08:00.
  --uninstall  Remove both the LaunchAgent and cron job installed by this tool.
  --help       Show this message.

The install directory is detected from this script's location, so the
scheduler works wherever you put gh_puller.
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
    PYTHON="${GH_PULLER_PYTHON:-python3}"
    "$PYTHON" - "$BASE" "$LABEL" "$PLIST_TEMPLATE" "$PLIST_PATH" <<'PY'
import pathlib
import sys

base, label, template_path, out_path = sys.argv[1:5]
tmpl = pathlib.Path(template_path).read_text()
out = tmpl.replace("@@BASE@@", base).replace("@@LABEL@@", label)
pathlib.Path(out_path).write_text(out)
PY
    chmod 644 "$PLIST_PATH"

    launchctl unload -w "$PLIST_PATH" 2>/dev/null || true
    launchctl load -w "$PLIST_PATH"
    echo "Installed LaunchAgent: $PLIST_PATH"
    echo "  Runs daily at 08:00 and whenever you log in."
}

uninstall_launchd() {
    if [[ "$OSTYPE" != darwin* ]]; then
        return 0
    fi
    if [[ -f "$PLIST_PATH" ]]; then
        launchctl unload -w "$PLIST_PATH" 2>/dev/null || true
        rm -f "$PLIST_PATH"
        echo "Removed LaunchAgent: $PLIST_PATH"
    fi
}

install_cron() {
    local tmp
    tmp="$(mktemp)"
    ( crontab -l 2>/dev/null || true ) | grep -vF "$CRON_MARKER" > "$tmp" || true
    cat >> "$tmp" <<EOF
$CRON_MARKER
0 8 * * * "$BASE/refresh.sh" all >> "$BASE/refresh.log" 2>&1
EOF
    crontab "$tmp"
    rm -f "$tmp"
    echo "Added cron job:"
    echo "  0 8 * * * \"$BASE/refresh.sh\" all >> \"$BASE/refresh.log\" 2>&1"
}

uninstall_cron() {
    local tmp
    tmp="$(mktemp)"
    ( crontab -l 2>/dev/null || true ) | grep -vF "$CRON_MARKER" > "$tmp" || true
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
