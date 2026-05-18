#!/bin/bash
# Runner used by cPanel cron — loads credentials from config.sh then runs
# whichever puller you ask for.
#
# Usage from cPanel cron:
#   bash /home/<user>/unfollow-ads/run.sh meta
#   bash /home/<user>/unfollow-ads/run.sh adjust
#   bash /home/<user>/unfollow-ads/run.sh rc

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load credentials (not in git — created from config.sh.example)
if [ ! -f "$SCRIPT_DIR/config.sh" ]; then
    echo "ERROR: $SCRIPT_DIR/config.sh missing. Copy config.sh.example, fill it in, then chmod 600 config.sh"
    exit 1
fi
# shellcheck disable=SC1091
source "$SCRIPT_DIR/config.sh"

# Find a Python 3.7+ interpreter. cPanel hosts often ship an old default
# `python3` (3.6 or even 2.x) but expose newer versions at known paths.
# Allow override via PYTHON_BIN in config.sh.
find_python() {
    if [ -n "$PYTHON_BIN" ] && [ -x "$PYTHON_BIN" ]; then
        echo "$PYTHON_BIN"
        return
    fi
    for candidate in \
        /opt/cpanel/ea-python311/root/usr/bin/python3.11 \
        /opt/cpanel/ea-python310/root/usr/bin/python3.10 \
        /opt/cpanel/ea-python39/root/usr/bin/python3.9 \
        /opt/alt/python311/bin/python3.11 \
        /opt/alt/python310/bin/python3.10 \
        /opt/alt/python39/bin/python3.9 \
        /usr/local/bin/python3.11 \
        /usr/local/bin/python3.10 \
        /usr/local/bin/python3.9 \
        /usr/bin/python3.11 \
        /usr/bin/python3.10 \
        /usr/bin/python3.9 \
        python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            # Verify it's Python 3.7+ (needed for our scripts)
            ver=$("$candidate" -c 'import sys; print("ok" if sys.version_info >= (3, 7) else "old")' 2>/dev/null || echo "old")
            if [ "$ver" = "ok" ]; then
                echo "$candidate"
                return
            fi
        fi
    done
    echo "ERROR: no Python 3.7+ found. Set PYTHON_BIN in config.sh." >&2
    exit 1
}

PY="$(find_python)"

# Log to a rolling file so we can debug if a cron run fails
LOG="$SCRIPT_DIR/cron.log"
TS="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"

case "$1" in
    meta)
        echo "[$TS] === Meta refresh (using $PY) ===" >> "$LOG"
        "$PY" "$SCRIPT_DIR/refresh_meta_ads.py" >> "$LOG" 2>&1
        ;;
    adjust)
        echo "[$TS] === Adjust refresh (using $PY) ===" >> "$LOG"
        "$PY" "$SCRIPT_DIR/refresh_adjust.py" >> "$LOG" 2>&1
        ;;
    rc)
        echo "[$TS] === RC dashboard refresh (using $PY) ===" >> "$LOG"
        "$PY" "$SCRIPT_DIR/refresh_dashboard_json.py" >> "$LOG" 2>&1
        ;;
    *)
        echo "Usage: $0 {meta|adjust|rc}" >&2
        exit 2
        ;;
esac
