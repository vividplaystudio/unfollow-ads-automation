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

# Maximum time a lock may be held before it's assumed to belong to a hung
# process. Real full-refresh runs take 5–30 min; 45 min is well above the
# tail and safe against variance. If flock is held past this age, we kill
# the incumbent's process group and reclaim.
LOCK_MAX_AGE_SEC=$((45 * 60))

# acquire_lock NAME: tries to take an exclusive flock on $LOCK_DIR/NAME.lock.
# Returns 0 (success, lock held by this shell) or 1 (lock held elsewhere).
#
# Two important properties:
#   * Failed acquisitions do NOT truncate the lock file. Prior code opened
#     with `>` on every attempt, wiping the pid the holder had recorded —
#     that's why old SKIPPED lines logged "pid " with no number. We use
#     `>>` (append) so the incumbent's info survives failed attempts.
#   * If the incumbent has been holding the lock for more than
#     LOCK_MAX_AGE_SEC we assume it's hung, kill its process group, and
#     reclaim. Without this the very issue we hit on 2026-07-01 recurs:
#     one dead full-refresh blocks all future runs indefinitely.
acquire_lock() {
    local name="$1"
    local lock="$LOCK_DIR/${name}.lock"

    # Append mode — never truncate the incumbent's info on a failed attempt.
    exec 9>>"$lock"

    if flock -n 9; then
        # Got it. NOW truncate and stamp with our identity + start time
        # so future callers can detect if we hang.
        : > "$lock"
        local pgid
        pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')"
        echo "pid=$$ pgid=${pgid:-$$} started_epoch=$(date -u +%s) started=$TS" >&9
        return 0
    fi

    # Locked. Parse the incumbent's info without modifying the file.
    local info holder holder_pgid started_epoch age_sec
    info="$(cat "$lock" 2>/dev/null || printf '')"
    holder="$(printf '%s' "$info" | sed -n 's/.*pid=\([0-9]*\).*/\1/p')"
    holder_pgid="$(printf '%s' "$info" | sed -n 's/.*pgid=\([0-9]*\).*/\1/p')"
    started_epoch="$(printf '%s' "$info" | sed -n 's/.*started_epoch=\([0-9]*\).*/\1/p')"
    age_sec=0
    if [ -n "$started_epoch" ] && [ "$started_epoch" -gt 0 ]; then
        age_sec=$(( $(date -u +%s) - started_epoch ))
    else
        # Old-format lock (no started_epoch) — fall back to file mtime.
        # This lets the new code reclaim locks written by the pre-fix
        # run.sh, which just contained a bare pid. Without this fallback,
        # a pre-existing hung run.sh could block forever.
        local mtime_epoch
        mtime_epoch="$(stat -c %Y "$lock" 2>/dev/null || echo 0)"
        if [ "$mtime_epoch" -gt 0 ]; then
            age_sec=$(( $(date -u +%s) - mtime_epoch ))
        fi
    fi

    # If the file has no pid (old buggy code truncated it on every failed
    # attempt), ask the OS who has the file open. This is the only way to
    # unstick the 2026-07-01 lock without manual intervention.
    #
    # fuser prints pids of ALL processes with the file open, INCLUDING
    # our own (we opened fd 9 above). Skip our own pid when picking a
    # holder. Fuser also appends letter suffixes (c=cwd, e=exe, f=file)
    # which we strip.
    if [ -z "$holder" ] && command -v fuser >/dev/null 2>&1; then
        local fuser_out pid
        fuser_out="$(fuser "$lock" 2>/dev/null || true)"
        for pid in $(printf '%s' "$fuser_out" | tr ' ' '\n' | sed 's/[a-zA-Z]$//' | grep -E '^[0-9]+$'); do
            if [ "$pid" != "$$" ]; then
                holder="$pid"
                break
            fi
        done
        if [ -n "$holder" ]; then
            holder_pgid="$(ps -o pgid= -p "$holder" 2>/dev/null | tr -d ' ')"
            # Anything the old buggy code touched is guaranteed to be
            # from a prior tick — force the stale branch to fire.
            [ "$age_sec" -lt "$LOCK_MAX_AGE_SEC" ] && age_sec="$LOCK_MAX_AGE_SEC"
        fi
    fi

    if [ "$age_sec" -ge "$LOCK_MAX_AGE_SEC" ] && [ -n "$holder" ]; then
        echo "[$TS] === ${name} STALE LOCK: pid=$holder pgid=${holder_pgid:-?} age=${age_sec}s (limit ${LOCK_MAX_AGE_SEC}s) — killing incumbent process group ===" >> "$LOG"
        if [ -n "$holder_pgid" ] && [ "$holder_pgid" != "0" ]; then
            kill -TERM -"$holder_pgid" 2>/dev/null || true
            sleep 3
            kill -KILL -"$holder_pgid" 2>/dev/null || true
        else
            kill -TERM "$holder" 2>/dev/null || true
            sleep 3
            kill -KILL "$holder" 2>/dev/null || true
        fi
        # Reopen fd (incumbent's death releases the flock).
        exec 9>>"$lock"
        if flock -n 9; then
            : > "$lock"
            local pgid
            pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')"
            echo "pid=$$ pgid=${pgid:-$$} started_epoch=$(date -u +%s) started=$TS" >&9
            echo "[$TS] === ${name} LOCK RECLAIMED from pid=$holder ===" >> "$LOG"
            return 0
        fi
        echo "[$TS] === ${name} still cannot acquire lock after kill attempt ===" >> "$LOG"
        return 1
    fi

    echo "[$TS] === ${name} SKIPPED: previous run still in progress (pid=${holder:-?} age=${age_sec}s) ===" >> "$LOG"
    return 1
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
