#!/usr/bin/env python3
"""One-off HISTORICAL backfill of Meta ad-set + campaign DAILY insights.

Pulls from BACKFILL_SINCE (default 2026-03-01) through today for EVERY ad
account, at daily granularity, chunked by month to stay under API row limits.

Why this captures REMOVED ad sets too:
  It reads the account-level insights edge (act_<id>/insights?level=adset),
  which returns a historical row for every entity that had delivery in the
  window — including ad sets later PAUSED, ARCHIVED, or DELETED. Meta retains
  insights ~37 months even after the object itself is gone. (The object edge
  /act_<id>/adsets would NOT return deleted ones — the insights edge does.)

Captures every action type + action_values, so whatever subscribe / purchase
events Meta recorded per ad set historically come through untouched.

Output: meta_history.json, uploaded to cPanel via the same FTP path the live
dashboard uses. Fetch it at https://genivox.com/ads-upload/meta_history.json

Reuses the live refresher's auth + fetchers so behavior is identical.
"""
import json
import os
from datetime import date, datetime, timedelta, timezone

import refresh_meta_ads as rm  # reuse token, fetch_insights, normalize_row, upload_to_ftp, fetch_statuses

SINCE = os.environ.get("BACKFILL_SINCE", "2026-03-01")
LEVELS = [x.strip() for x in os.environ.get("BACKFILL_LEVELS", "adset,campaign").split(",") if x.strip()]
OUTPUT = "meta_history.json"


def month_windows(since_str: str, until_d: date) -> list:
    """Break [since, today] into per-calendar-month [start, end] pairs."""
    cur = date.fromisoformat(since_str)
    out = []
    while cur <= until_d:
        nxt = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        out.append((cur.isoformat(), min(nxt - timedelta(days=1), until_d).isoformat()))
        cur = nxt
    return out


def main() -> None:
    today = datetime.now(timezone.utc).date()
    windows = month_windows(SINCE, today)
    print(f"▶ Meta HISTORY backfill {SINCE} → {today}")
    print(f"  accounts: {rm.META_AD_ACCOUNT_IDS}")
    print(f"  levels:   {LEVELS}")
    print(f"  windows:  {len(windows)} monthly chunks")

    daily = {lvl: [] for lvl in LEVELS}
    for aid in rm.META_AD_ACCOUNT_IDS:
        print(f"\n── Account act_{aid} ──")
        for since, until in windows:
            for lvl in LEVELS:
                try:
                    rows = rm.fetch_insights(aid, lvl, since, until, time_increment=1)
                except Exception as e:  # noqa: BLE001 — keep going, log the gap
                    print(f"  ! {lvl:7} {since}..{until} ERROR: {e}")
                    continue
                for r in rows:
                    nr = rm.normalize_row(r)
                    nr["account_id"] = aid
                    daily[lvl].append(nr)
                if rows:
                    print(f"  {lvl:7} {since}..{until}: {len(rows)} rows")

    # Current on/off status of still-existing entities (deleted ones simply
    # won't appear here — but their historical rows are already in `daily`).
    statuses = {}
    for aid in rm.META_AD_ACCOUNT_IDS:
        try:
            statuses[aid] = {
                "campaigns": rm.fetch_statuses(aid, "campaigns"),
                "adsets": rm.fetch_statuses(aid, "adsets"),
            }
        except Exception as e:  # noqa: BLE001
            print(f"  ! statuses {aid} ERROR: {e}")
            statuses[aid] = {}

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "since": SINCE,
        "until": today.isoformat(),
        "account_ids": rm.META_AD_ACCOUNT_IDS,
        "levels": LEVELS,
        "adsets_daily": daily.get("adset", []),
        "campaigns_daily": daily.get("campaign", []),
        "ads_daily": daily.get("ad", []),
        "statuses_by_account": statuses,
    }
    with open(OUTPUT, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    n_ad = len(payload["adsets_daily"])
    n_cm = len(payload["campaigns_daily"])
    uniq_adsets = len({(r.get("account_id"), r.get("adset_id")) for r in payload["adsets_daily"]})
    print(f"\n✓ {OUTPUT}: {n_ad} adset-day rows ({uniq_adsets} distinct ad sets), {n_cm} campaign-day rows")

    rm.upload_to_ftp(OUTPUT, OUTPUT)


if __name__ == "__main__":
    main()
