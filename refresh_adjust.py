#!/usr/bin/env python3
"""
Adjust Reports Service refresher.

Pulls per-creative install / subscribe / revenue data from Adjust, broken
down per event token (Com_Monthly, Com_Weekly, Com_Yearly) so the dashboard
can show real ROAS and product mix per Meta ad / ASA keyword / etc.

Joins with Meta on creative_id_network → meta ad_id (Adjust copies Meta's
numeric ad_id into the creative dimension when MMP attribution is set up).

Outputs adjust.json and FTPs it to cPanel alongside data.json / meta_ads.json.
"""

import ftplib
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


ADJUST_API_TOKEN = os.environ["ADJUST_API_TOKEN"]
ADJUST_APP_TOKEN = os.environ["ADJUST_APP_TOKEN"]

FTP_HOST = os.environ.get("FTP_HOST", "")
FTP_USER = os.environ.get("FTP_USER", "")
FTP_PASS = os.environ.get("FTP_PASS", "")
FTP_PATH = os.environ.get("FTP_PATH", "/public_html/ads-dashboard")

OUTPUT_FILE = "adjust.json"

# Live paywall event tokens.
EVENT_TOKENS = {
    "Com_Monthly": "7i6tu0",   # $9.99 monthly
    "Com_Weekly":  "taj6y9",   # $4.99 weekly
    "Com_Yearly":  "xykp3h",   # $22.99 yearly
}

# Base metrics every report pulls.
BASE_METRICS = [
    "installs",
    "clicks",
    "impressions",
    "all_revenue",
    "paid_revenue",
    "events",
]


def event_metrics() -> list:
    """Per-event count + revenue metrics for Adjust Reports Service."""
    out = []
    for token in EVENT_TOKENS.values():
        out.append(f"{token}_events")
        out.append(f"{token}_revenue")
    return out


# ══════════════════════════════════════════════════════════════════
# Adjust API
# ══════════════════════════════════════════════════════════════════

def adjust_get(params: dict) -> dict:
    """GET against Adjust Reports Service. Returns parsed JSON."""
    url = (
        "https://dash.adjust.com/control-center/reports-service/report?"
        + urllib.parse.urlencode(params, doseq=True)
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {ADJUST_API_TOKEN}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  ❌ Adjust API error {e.code}: {body[:800]}")
        raise


def fetch_report(since: str, until: str, dimensions: list, extra: dict = None) -> list:
    metrics = BASE_METRICS + event_metrics()
    params = {
        "app_token__in": ADJUST_APP_TOKEN,
        "date_period": f"{since}:{until}",
        "dimensions": ",".join(dimensions),
        "metrics": ",".join(metrics),
    }
    if extra:
        params.update(extra)
    return adjust_get(params).get("rows", [])


# ══════════════════════════════════════════════════════════════════
# FTP upload (mirrors refresh_meta_ads.py)
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

    print(f"▶ Adjust refresh for app {ADJUST_APP_TOKEN[:6]}…")

    print("  Fetching summary windows (network breakdown)…")
    summary = {
        "today":         fetch_report(d(0), d(0), ["network"]),
        "yesterday":     fetch_report(d(1), d(1), ["network"]),
        "last_7_days":   fetch_report(d(6), d(0), ["network"]),
        "last_30_days":  fetch_report(d(29), d(0), ["network"]),
    }
    for k, rows in summary.items():
        print(f"    {k}: {len(rows)} network rows")

    print("  Fetching per-campaign 30d…")
    by_campaign = fetch_report(d(29), d(0), ["network", "campaign"])
    print(f"    {len(by_campaign)} campaign rows")

    print("  Fetching per-adgroup 30d…")
    by_adgroup = fetch_report(d(29), d(0), ["network", "campaign", "adgroup"])
    print(f"    {len(by_adgroup)} adgroup rows")

    print("  Fetching per-creative 30d…")
    by_creative = fetch_report(d(29), d(0), ["network", "campaign", "adgroup", "creative"])
    print(f"    {len(by_creative)} creative rows")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "app_token": ADJUST_APP_TOKEN,
        "event_tokens": EVENT_TOKENS,
        "summary": summary,
        "by_campaign": by_campaign,
        "by_adgroup": by_adgroup,
        "by_creative": by_creative,
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
