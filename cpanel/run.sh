#!/bin/bash
# Runner used by cPanel cron — loads credentials from config.sh then runs
# whichever puller you ask for.
#
# Usage from cPanel cron:
#   bash /home/<user>/unfollow-ads/run.sh meta
#   bash /home/<user>/unfollow-ads/run.sh adjust
#   bash /home/<user>/unfollow-ads/run.sh rc       — full refresh (slow, ~10 min)
#   bash /home/<user>/unfollow-ads/run.sh rc-fast  — daily_rc only (fast, ~5 s)

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

# Lock directory — we lock per-step (not per-cron-arg) so the `rc` cron
# can keep refreshing daily_rc via the fast pre-step even when the slow
# full refresh is still running from a prior tick.
LOCK_DIR="$SCRIPT_DIR/locks"
mkdir -p "$LOCK_DIR"

# acquire_lock NAME: tries to take an exclusive flock on $LOCK_DIR/NAME.lock.
# Returns 0 (success, lock held by this shell) or 1 (lock already held by
# another process). On success, the lock is auto-released when this script
# exits. On failure, a SKIPPED line is appended to the cron log.
acquire_lock() {
    local name="$1"
    local lock="$LOCK_DIR/${name}.lock"
    # fd 9 reserved for the per-call lock; reopen for each call
    exec 9>"$lock"
    if ! flock -n 9; then
        local holder
        holder="$(cat "$lock" 2>/dev/null || echo "?")"
        echo "[$TS] === ${name} SKIPPED: previous run still in progress (pid $holder) ===" >> "$LOG"
        return 1
    fi
    echo "$$" >&9
    return 0
}

# Run Python unbuffered (-u) so progress prints appear in cron.log in real
# time — without this, prints stay in stdout buffer until the script exits
# and we can't tell a hung run apart from a slow one.
PYFLAGS="-u"

case "$1" in
    meta)
        acquire_lock meta || exit 0
        echo "[$TS] === Meta refresh (using $PY) ===" >> "$LOG"
        "$PY" $PYFLAGS "$SCRIPT_DIR/refresh_meta_ads.py" >> "$LOG" 2>&1
        ;;
    adjust)
        acquire_lock adjust || exit 0
        echo "[$TS] === Adjust refresh (using $PY) ===" >> "$LOG"
        "$PY" $PYFLAGS "$SCRIPT_DIR/refresh_adjust.py" >> "$LOG" 2>&1
        ;;
    rc)
        # Fast pre-step: patch only daily_rc (~5 s). Runs unconditionally —
        # the script itself writes data.json atomically and is safe to run
        # in parallel with anything else. Failure here is non-fatal; we
        # still attempt the full refresh below.
        echo "[$TS] === rc fast pre-step ===" >> "$LOG"
        "$PY" $PYFLAGS "$SCRIPT_DIR/refresh_daily_rc_fast.py" >> "$LOG" 2>&1 || \
            echo "  (fast pre-step failed — full refresh will still attempt)" >> "$LOG"
        # Full refresh is what we lock against. If the previous tick is
        # still running it (slow RC API), skip rather than pile on.
        acquire_lock rc-full || exit 0
        echo "[$TS] === RC dashboard refresh (using $PY) ===" >> "$LOG"
        "$PY" $PYFLAGS "$SCRIPT_DIR/refresh_dashboard_json.py" >> "$LOG" 2>&1
        ;;
    rc-fast)
        acquire_lock rc-fast || exit 0
        echo "[$TS] === RC fast daily_rc refresh (using $PY) ===" >> "$LOG"
        "$PY" $PYFLAGS "$SCRIPT_DIR/refresh_daily_rc_fast.py" >> "$LOG" 2>&1
        ;;
    *)
        echo "Usage: $0 {meta|adjust|rc|rc-fast}" >&2
        exit 2
        ;;
esac
