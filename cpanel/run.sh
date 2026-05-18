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

# Log to a rolling file so we can debug if a cron run fails
LOG="$SCRIPT_DIR/cron.log"
TS="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"

case "$1" in
    meta)
        echo "[$TS] === Meta refresh ===" >> "$LOG"
        python3 "$SCRIPT_DIR/refresh_meta_ads.py" >> "$LOG" 2>&1
        ;;
    adjust)
        echo "[$TS] === Adjust refresh ===" >> "$LOG"
        python3 "$SCRIPT_DIR/refresh_adjust.py" >> "$LOG" 2>&1
        ;;
    rc)
        echo "[$TS] === RC dashboard refresh ===" >> "$LOG"
        python3 "$SCRIPT_DIR/refresh_dashboard_json.py" >> "$LOG" 2>&1
        ;;
    *)
        echo "Usage: $0 {meta|adjust|rc}" >&2
        exit 2
        ;;
esac
