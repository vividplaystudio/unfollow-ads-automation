#!/usr/bin/env python3
"""
Meta Ads Data Refresher — pulls Meta Marketing API insights at the
ad / adset / campaign level and writes meta_ads.json to cPanel via FTP.

Solves the limitation of the Adjust dashboard, which only shows campaign-
level data and lags spend by 24h+. Meta's own API exposes per-ad spend
in near real-time (typically <1h delay).

Outputs:
- summary KPIs (today, yesterday, last 7d, last 30d)
- per-ad daily breakdown for the last 30 days
- joined hierarchy: campaign → adset → ad

Runs hourly via GitHub Actions alongside refresh_dashboard_json.py.
"""

import ftplib
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


META_ACCESS_TOKEN = os.environ["META_ACCESS_TOKEN"]
META_AD_ACCOUNT_ID = os.environ.get("META_AD_ACCOUNT_ID", "2399779997191076")
META_API_VERSION = os.environ.get("META_API_VERSION", "v19.0")

FTP_HOST = os.environ.get("FTP_HOST", "")
FTP_USER = os.environ.get("FTP_USER", "")
FTP_PASS = os.environ.get("FTP_PASS", "")
FTP_PATH = os.environ.get("FTP_PATH", "/public_html/ads-dashboard")

OUTPUT_FILE = "meta_ads.json"

INSIGHT_FIELDS = [
    "campaign_id",
    "campaign_name",
    "adset_id",
    "adset_name",
    "ad_id",
    "ad_name",
    "spend",
    "impressions",
    "clicks",
    "reach",
    "ctr",
    "cpc",
    "cpm",
    "frequency",
    "actions",
    "action_values",
    "cost_per_action_type",
]

# Action types we care about for a subscription app
TRACKED_ACTION_TYPES = {
    "mobile_app_install",
    "app_install",
    "omni_app_install",
    "app_custom_event.fb_mobile_purchase",
    "purchase",
    "omni_purchase",
    "subscribe",
    "start_trial",
    "app_custom_event.fb_mobile_initiated_checkout",
    "app_custom_event.fb_mobile_complete_registration",
}


# ══════════════════════════════════════════════════════════════════
# Meta Graph API helpers
# ══════════════════════════════════════════════════════════════════

def meta_get(path: str, params: dict) -> dict:
    """GET against Meta Graph API. Returns parsed JSON."""
    params = {**params, "access_token": META_ACCESS_TOKEN}
    url = f"https://graph.facebook.com/{META_API_VERSION}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  ❌ Meta API error {e.code}: {body[:500]}")
        raise


def meta_paginated(path: str, params: dict) -> list:
    """Follow paging.next until exhausted. Returns concatenated data array."""
    out = []
    page = meta_get(path, params)
    while True:
        out.extend(page.get("data", []))
        next_url = page.get("paging", {}).get("next")
        if not next_url:
            break
        req = urllib.request.Request(next_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            page = json.loads(resp.read().decode("utf-8"))
    return out


# ══════════════════════════════════════════════════════════════════
# Insights fetchers
# ══════════════════════════════════════════════════════════════════

def fetch_insights(level: str, since: str, until: str, time_increment: str | int = "all_days") -> list:
    """Fetch insights at the given level and date range."""
    params = {
        "level": level,
        "fields": ",".join(INSIGHT_FIELDS),
        "time_range": json.dumps({"since": since, "until": until}),
        "time_increment": str(time_increment),
        "limit": 500,
    }
    return meta_paginated(f"act_{META_AD_ACCOUNT_ID}/insights", params)


def normalize_actions(row: dict) -> dict:
    """Flatten the messy actions / action_values arrays into named columns."""
    out = {}
    for a in row.get("actions", []) or []:
        t = a.get("action_type")
        if t in TRACKED_ACTION_TYPES:
            out[f"action_{t}"] = float(a.get("value", 0))
    for a in row.get("action_values", []) or []:
        t = a.get("action_type")
        if t in TRACKED_ACTION_TYPES:
            out[f"value_{t}"] = float(a.get("value", 0))
    return out


def normalize_row(row: dict) -> dict:
    """Coerce numeric fields and flatten actions."""
    out = {
        "date": row.get("date_start"),
        "campaign_id": row.get("campaign_id"),
        "campaign_name": row.get("campaign_name"),
        "adset_id": row.get("adset_id"),
        "adset_name": row.get("adset_name"),
        "ad_id": row.get("ad_id"),
        "ad_name": row.get("ad_name"),
        "spend": float(row.get("spend", 0) or 0),
        "impressions": int(row.get("impressions", 0) or 0),
        "clicks": int(row.get("clicks", 0) or 0),
        "reach": int(row.get("reach", 0) or 0),
        "ctr": float(row.get("ctr", 0) or 0),
        "cpc": float(row.get("cpc", 0) or 0),
        "cpm": float(row.get("cpm", 0) or 0),
        "frequency": float(row.get("frequency", 0) or 0),
    }
    out.update(normalize_actions(row))
    return out


def summarize(rows: list) -> dict:
    """Aggregate a list of insight rows into one summary dict."""
    s = {"spend": 0.0, "impressions": 0, "clicks": 0, "reach": 0}
    actions = {}
    for r in rows:
        s["spend"] += float(r.get("spend", 0) or 0)
        s["impressions"] += int(r.get("impressions", 0) or 0)
        s["clicks"] += int(r.get("clicks", 0) or 0)
        s["reach"] += int(r.get("reach", 0) or 0)
        for a in r.get("actions", []) or []:
            t = a.get("action_type")
            if t in TRACKED_ACTION_TYPES:
                actions[t] = actions.get(t, 0.0) + float(a.get("value", 0))
    s["actions"] = actions
    s["cpm"] = (s["spend"] / s["impressions"] * 1000) if s["impressions"] else 0
    s["cpc"] = (s["spend"] / s["clicks"]) if s["clicks"] else 0
    s["ctr"] = (s["clicks"] / s["impressions"] * 100) if s["impressions"] else 0
    return s


# ══════════════════════════════════════════════════════════════════
# FTP upload (mirrors refresh_dashboard_json.py)
# ══════════════════════════════════════════════════════════════════

def upload_to_ftp(local_file: str, remote_name: str) -> None:
    if not FTP_HOST:
        print("  [skip FTP — no credentials]")
        return

    print(f"  Uploading to {FTP_HOST}:{FTP_PATH}/{remote_name}")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    ftp = ftplib.FTP_TLS(FTP_HOST, timeout=60, context=ctx)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.prot_p()

    try:
        ftp.cwd(FTP_PATH)
    except ftplib.error_perm:
        parts = FTP_PATH.strip("/").split("/")
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else f"/{part}"
            try:
                ftp.cwd(current)
            except ftplib.error_perm:
                ftp.mkd(current)
                ftp.cwd(current)

    with open(local_file, "rb") as f:
        ftp.storbinary(f"STOR {remote_name}", f)
    ftp.quit()
    print(f"    ✅ Uploaded {remote_name}")


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

def main() -> None:
    today = datetime.now(timezone.utc).date()
    d = lambda offset: (today - timedelta(days=offset)).isoformat()

    print(f"▶ Meta Ads refresh for act_{META_AD_ACCOUNT_ID}")

    print("  Fetching summary windows…")
    summary = {
        "today": summarize(fetch_insights("account", d(0), d(0))),
        "yesterday": summarize(fetch_insights("account", d(1), d(1))),
        "last_7_days": summarize(fetch_insights("account", d(6), d(0))),
        "last_30_days": summarize(fetch_insights("account", d(29), d(0))),
    }

    print("  Fetching per-ad daily breakdown (last 30d)…")
    ad_rows_raw = fetch_insights("ad", d(29), d(0), time_increment=1)
    ad_rows = [normalize_row(r) for r in ad_rows_raw]
    print(f"    {len(ad_rows)} ad-day rows")

    print("  Fetching per-adset 30d totals…")
    adset_rows = [normalize_row(r) for r in fetch_insights("adset", d(29), d(0))]

    print("  Fetching per-campaign 30d totals…")
    campaign_rows = [normalize_row(r) for r in fetch_insights("campaign", d(29), d(0))]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account_id": META_AD_ACCOUNT_ID,
        "summary": summary,
        "campaigns": campaign_rows,
        "adsets": adset_rows,
        "ads": ad_rows,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"  Wrote {OUTPUT_FILE} ({os.path.getsize(OUTPUT_FILE):,} bytes)")

    upload_to_ftp(OUTPUT_FILE, OUTPUT_FILE)
    print("✅ Done")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ Fatal: {e}", file=sys.stderr)
        sys.exit(1)
