#!/usr/bin/env python3
"""
Fast standalone refresh of the daily_rc field in data.json.

Why this exists
---------------
The full refresh_dashboard_json.py walks every RC customer (~36k) and
calls RC's v2 API for each one to enrich attributes + subscriptions. When
RC's API is sluggish (which has become routine), that step takes 10–30
minutes and sometimes hangs entirely. While it's hung, the True Daily
Profit cards on the dashboard show data that's hours stale — exactly the
silent-staleness problem we are trying to eliminate.

The dashboard's True Daily Profit only needs the daily_rc field. And
daily_rc can be built end-to-end from RC webhook events, which we
already capture in rc_events.jsonl on cPanel. This script does only that
small slice in ~5 seconds:

    1. Fetch the last 60 days of webhook events (cheap — one HTTP call).
    2. Build a synthetic "customers" list ({_transactions: [...]} each).
    3. Reuse the existing compute_daily_rc from the full script.
    4. Atomically patch data.json's daily_rc field in place.

We deliberately do NOT touch any other field. Campaign/keyword/channel
tables continue to come from the full refresh (which can stay slow or
broken without affecting True Net).

Safety guarantees
-----------------
* If webhook fetch returns 0 events we abort instead of wiping daily_rc.
* We write data.json atomically (temp file + rename) so a crashed run
  cannot leave a half-written file.
* We require LOCAL_OUTPUT_DIR (only meaningful on cPanel) — running this
  on a dev box without that env is a no-op error, not a destructive
  write.

Usage (called from cpanel/run.sh):
    python3 refresh_daily_rc_fast.py
"""

import json
import os
import sys
from datetime import datetime, timezone

# Reuse the exact same webhook-fetch + bucketing logic as the full
# refresh so the fast path can never disagree with what the slow path
# would have written.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from refresh_dashboard_json import (  # noqa: E402
    fetch_webhook_events,
    compute_daily_rc,
    validate_daily_rc,
    LOCAL_OUTPUT_DIR,
    RC_WEBHOOK_SECRET,
)


def main() -> int:
    ts = datetime.now(timezone.utc).isoformat()
    print(f"\n=== Fast daily_rc refresh @ {ts} ===")

    if not RC_WEBHOOK_SECRET:
        print("ERROR: RC_WEBHOOK_SECRET not set", file=sys.stderr)
        return 1

    # We require LOCAL_OUTPUT_DIR. The fast path is cPanel-only by design;
    # it patches data.json in place rather than re-uploading via FTP so
    # there is no race with the full refresh's upload step.
    if not LOCAL_OUTPUT_DIR:
        print(
            "ERROR: LOCAL_OUTPUT_DIR not set — refresh_daily_rc_fast.py "
            "must run on the cPanel host where data.json lives",
            file=sys.stderr,
        )
        return 1

    data_path = os.path.join(LOCAL_OUTPUT_DIR, "data.json")
    if not os.path.exists(data_path):
        print(
            f"ERROR: {data_path} not found — run the full refresh first",
            file=sys.stderr,
        )
        return 1

    # 1. Fetch webhook events (fast: single HTTP call to PHP endpoint).
    print("→ Fetching webhook events...")
    webhook_events = fetch_webhook_events()

    # Defensive: never overwrite a good daily_rc with empty data. A 0-event
    # response usually means a transient PHP / auth error, not "no revenue
    # happened." The right move is to leave data.json alone and try again
    # next cron tick.
    total_events = sum(len(v) for v in webhook_events.values())
    if not webhook_events or total_events == 0:
        print(
            "ERROR: webhook fetch returned 0 events. Refusing to overwrite "
            "daily_rc with empty data. Existing data.json left untouched.",
            file=sys.stderr,
        )
        return 1

    print(f"  Got {total_events} events for {len(webhook_events)} users")

    # 2. Build synthetic customer list — compute_daily_rc only reads the
    # _transactions field, so a dict with that key is all it needs.
    synthetic_customers = [
        {"_transactions": txns} for txns in webhook_events.values()
    ]

    # 3. Compute daily_rc using the EXACT same logic the full refresh uses.
    print("→ Computing daily_rc...")
    daily_rc = compute_daily_rc(synthetic_customers, days=30)
    if not daily_rc:
        print(
            "ERROR: compute_daily_rc returned empty. Refusing to write.",
            file=sys.stderr,
        )
        return 1

    latest = daily_rc[-1]
    print(
        f"  Built {len(daily_rc)} days; latest: ${latest['revenue']:.2f} "
        f"on {latest['date']} ({latest['new_subs']}N + {latest['renewals']}R)"
    )

    # 4. Load existing data.json and patch only the daily_rc field. We
    # intentionally do NOT touch the top-level last_updated (which
    # represents the full refresh's timestamp) so the dashboard can still
    # tell which fields came from the slow path vs. the fast path.
    print("→ Patching data.json...")
    with open(data_path, "r") as f:
        data = json.load(f)

    # 4a. Validate the new daily_rc against the existing one BEFORE we
    # patch. If it would regress (zeros on settled days, big drops), keep
    # the existing daily_rc and exit non-zero so the cron log shows the
    # rejection. This is the gate that stopped earlier today's bug
    # (compute_daily_rc returning zeros) from reaching the dashboard.
    prev_daily_rc = data.get("daily_rc") or []
    ok, reason = validate_daily_rc(daily_rc, prev_daily_rc, source="rc-fast")
    print(f"  validate: {reason}")
    if not ok:
        print(
            "ERROR: refusing to overwrite daily_rc with broken data. "
            "Existing data.json left untouched.",
            file=sys.stderr,
        )
        return 1

    data["daily_rc"] = daily_rc
    data["daily_rc_updated_at"] = ts

    # 5. Atomic write: temp file + rename. Avoids leaving a partial JSON
    # if the script is killed mid-write.
    tmp_path = data_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp_path, data_path)

    size = os.path.getsize(data_path)
    print(f"  ✅ Wrote {data_path} ({size:,} bytes)")
    print("\n🎉 Done.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
