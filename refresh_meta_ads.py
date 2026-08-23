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
import socket
import ssl
import sys
import time
import urllib.parse
from typing import Union
import urllib.request
from datetime import datetime, timedelta, timezone


META_ACCESS_TOKEN = os.environ["META_ACCESS_TOKEN"]

# Support one OR many ad accounts under the same Business Manager. Set
# META_AD_ACCOUNT_IDS to a comma-separated list ("acc1,acc2,acc3") to pull
# all of them into one meta_ads.json. Falls back to the single-account
# META_AD_ACCOUNT_ID env for backward compat with the pre-multi-account
# install. Both env vars accept IDs with or without the "act_" prefix —
# whitespace is trimmed.
def _parse_account_ids() -> list:
    raw = os.environ.get("META_AD_ACCOUNT_IDS") or os.environ.get("META_AD_ACCOUNT_ID", "2399779997191076")
    ids = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if item.startswith("act_"):
            item = item[4:]
        ids.append(item)
    if not ids:
        raise RuntimeError("No Meta ad account IDs configured")
    return ids

META_AD_ACCOUNT_IDS = _parse_account_ids()
# Kept for any downstream code that still references the singular name.
META_AD_ACCOUNT_ID = META_AD_ACCOUNT_IDS[0]
META_API_VERSION = os.environ.get("META_API_VERSION", "v19.0")

# When running ON the cPanel host, set LOCAL_OUTPUT_DIR to the absolute
# dashboard folder (e.g., /home/genivox/public_html/ads-dashboard) and
# the script will write directly there — skipping FTP entirely.
LOCAL_OUTPUT_DIR = os.environ.get("LOCAL_OUTPUT_DIR", "")

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
    "inline_link_clicks",
    "inline_link_click_ctr",
    "cost_per_inline_link_click",
    "actions",
    "action_values",
    "cost_per_action_type",
]

# Capture every action type Meta returns. The Meta Ads Manager UI shows
# results as "in-app subscribes" but the API field can be any of several
# variants (subscribe, app_custom_event.fb_mobile_subscribe, omni_subscribe,
# app_custom_event.SUBSCRIBE, etc.) depending on how the event is logged.
# Whitelist filtering hides these — pull everything and let the dashboard
# pick the right key.
TRACKED_ACTION_TYPES = None  # None = no filter, keep all action types


# ══════════════════════════════════════════════════════════════════
# Meta Graph API helpers
# ══════════════════════════════════════════════════════════════════

# Meta error codes that mean "ask again later", not "your request is wrong".
# 4/17/32/613 are the rate-limit family (app-, user-, page- and custom-level),
# 1 and 2 are Meta's transient/unknown bucket, 341 is a temporary app cap.
_TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 613}
_RETRY_WAITS = (30, 60, 120)  # rate limits reset on the order of minutes


def _is_transient(status: int, body: str) -> bool:
    """Decide whether a failed Graph call is worth repeating.

    Meta signals this inconsistently: sometimes is_transient, sometimes only a
    code, and rate limits arrive as 403 rather than 429. A 403 that carries no
    recognisable code is still treated as transient because the ones observed
    in this pipeline ("Request body is not readable", "Application request
    limit reached") both were, and a genuinely bad token fails every retry and
    surfaces anyway a few minutes later.
    """
    if status in (500, 502, 503, 504):
        return True
    try:
        err = json.loads(body).get("error", {})
    except (ValueError, AttributeError):
        return status == 403
    if err.get("is_transient"):
        return True
    if err.get("code") in _TRANSIENT_CODES:
        return True
    return status == 403


def meta_get(path: str, params: dict) -> dict:
    """GET against Meta Graph API, retrying transient failures.

    Without this a single rate-limit blip killed the whole dashboard refresh
    and sent a failure email, even though the next run 20 minutes later
    succeeded untouched. Permanent errors (bad token, malformed field) still
    raise on the first attempt, so real breakage is not buried in retries.
    """
    params = {**params, "access_token": META_ACCESS_TOKEN}
    url = f"https://graph.facebook.com/{META_API_VERSION}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    for attempt in range(len(_RETRY_WAITS) + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            retryable, label, exc = _is_transient(e.code, body), f"Meta API error {e.code}", e
        except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
            body, retryable, label, exc = str(e), True, "Meta API unreachable", e
        # Re-raise explicitly: a bare `raise` here is outside the except block,
        # so there is no active exception to propagate.
        if not retryable or attempt == len(_RETRY_WAITS):
            print(f"  ❌ {label}: {body[:500]}")
            raise exc
        wait = _RETRY_WAITS[attempt]
        print(f"  ⚠️  {label} (transient) — retrying in {wait}s "
              f"[{attempt + 1}/{len(_RETRY_WAITS)}]: {body[:200]}")
        time.sleep(wait)


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

def fetch_insights(account_id: str, level: str, since: str, until: str, time_increment: Union[str, int] = "all_days") -> list:
    """Fetch insights at the given level and date range for one ad account.
    Use the unified attribution setting + the same windows Ads Manager UI shows
    (7-day click + 1-day view) so 'Results' / 'in-app subscribes' line up with
    what the user sees in Meta Ads Manager."""
    params = {
        "level": level,
        "fields": ",".join(INSIGHT_FIELDS),
        "time_range": json.dumps({"since": since, "until": until}),
        "time_increment": str(time_increment),
        "limit": 500,
        "use_unified_attribution_setting": "true",
        "action_attribution_windows": json.dumps(["7d_click", "1d_view"]),
        "action_report_time": "conversion",
    }
    return meta_paginated(f"act_{account_id}/insights", params)


def normalize_actions(row: dict) -> dict:
    """Flatten the messy actions / action_values arrays into named columns.
    Captures every action type returned (no whitelist)."""
    out = {}
    for a in row.get("actions", []) or []:
        t = a.get("action_type")
        if not t:
            continue
        if TRACKED_ACTION_TYPES is None or t in TRACKED_ACTION_TYPES:
            out[f"action_{t}"] = float(a.get("value", 0))
    for a in row.get("action_values", []) or []:
        t = a.get("action_type")
        if not t:
            continue
        if TRACKED_ACTION_TYPES is None or t in TRACKED_ACTION_TYPES:
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
        "inline_link_clicks": int(row.get("inline_link_clicks", 0) or 0),
        "inline_link_click_ctr": float(row.get("inline_link_click_ctr", 0) or 0),
        "cost_per_inline_link_click": float(row.get("cost_per_inline_link_click", 0) or 0),
    }
    out.update(normalize_actions(row))
    return out


def summarize(rows: list) -> dict:
    """Aggregate a list of insight rows into one summary dict."""
    s = {"spend": 0.0, "impressions": 0, "clicks": 0, "reach": 0, "link_clicks": 0}
    actions = {}
    freq_weighted = 0.0
    for r in rows:
        spend = float(r.get("spend", 0) or 0)
        impr = int(r.get("impressions", 0) or 0)
        s["spend"] += spend
        s["impressions"] += impr
        s["clicks"] += int(r.get("clicks", 0) or 0)
        s["reach"] += int(r.get("reach", 0) or 0)
        s["link_clicks"] += int(r.get("inline_link_clicks", 0) or 0)
        freq_weighted += float(r.get("frequency", 0) or 0) * impr
        for a in r.get("actions", []) or []:
            t = a.get("action_type")
            if not t:
                continue
            if TRACKED_ACTION_TYPES is None or t in TRACKED_ACTION_TYPES:
                actions[t] = actions.get(t, 0.0) + float(a.get("value", 0))
    s["actions"] = actions
    s["cpm"] = (s["spend"] / s["impressions"] * 1000) if s["impressions"] else 0
    s["cpc"] = (s["spend"] / s["clicks"]) if s["clicks"] else 0
    s["ctr"] = (s["clicks"] / s["impressions"] * 100) if s["impressions"] else 0
    s["link_ctr"] = (s["link_clicks"] / s["impressions"] * 100) if s["impressions"] else 0
    s["cost_per_link_click"] = (s["spend"] / s["link_clicks"]) if s["link_clicks"] else 0
    s["frequency"] = (freq_weighted / s["impressions"]) if s["impressions"] else 0
    return s


# ══════════════════════════════════════════════════════════════════
# Status (on/off/paused) fetchers
# ══════════════════════════════════════════════════════════════════

def fetch_statuses(account_id: str, level: str) -> dict:
    """Fetch id → {status, effective_status, name} for every campaign/
    adset/ad in ONE account. Used by the dashboard to flag paused items.

    level: 'campaigns' | 'adsets' | 'ads'
    """
    # Budgets live on the campaign/adset OBJECT, never on insights — which is
    # why the row-level tables carry no budget column. Pull them here so every
    # consumer can compare spend against the cap that produced it.
    fields = "id,name,status,effective_status"
    if level in ("campaigns", "adsets"):
        fields += ",daily_budget,lifetime_budget"
    params = {
        "fields": fields,
        "limit": 500,
    }
    rows = meta_paginated(f"act_{account_id}/{level}", params)

    def _money(v):
        """Meta returns budgets in minor units (cents). '5000' -> 50.0."""
        if v in (None, ""):
            return None
        try:
            return round(int(v) / 100.0, 2)
        except (TypeError, ValueError):
            return None

    out = {}
    for r in rows:
        entry = {
            "name": r.get("name", ""),
            "status": r.get("status", ""),
            "effective_status": r.get("effective_status", ""),
        }
        if level in ("campaigns", "adsets"):
            entry["daily_budget"] = _money(r.get("daily_budget"))
            entry["lifetime_budget"] = _money(r.get("lifetime_budget"))
        out[r["id"]] = entry
    return out


# ══════════════════════════════════════════════════════════════════
# FTP upload (mirrors refresh_dashboard_json.py)
# ══════════════════════════════════════════════════════════════════

def publish_output(local_file: str, remote_name: str) -> None:
    """Publish the JSON to the dashboard. When LOCAL_OUTPUT_DIR is set
    (script is running on the cPanel host), copy directly — no FTP.
    Otherwise fall back to FTPS upload."""
    if LOCAL_OUTPUT_DIR:
        import shutil
        os.makedirs(LOCAL_OUTPUT_DIR, exist_ok=True)
        target = os.path.join(LOCAL_OUTPUT_DIR, remote_name)
        shutil.copyfile(local_file, target)
        print(f"    ✅ Copied to {target}")
        return
    upload_to_ftp(local_file, remote_name)


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

    # Per-account pulls collected here, then merged into one payload so
    # the dashboard can show a single view or filter by account_id.
    # Every row is tagged with `account_id` at normalize time so downstream
    # can filter/group without needing a separate index.
    combined_campaigns: list = []
    combined_adsets: list = []
    combined_ads: list = []
    combined_statuses: dict = {}  # { <account_id>: {campaigns:{}, adsets:{}, ads:{}} }
    per_account_summary: dict = {}  # { <account_id>: {today, yesterday, ...} }

    print(f"▶ Meta Ads refresh for {len(META_AD_ACCOUNT_IDS)} account(s): {META_AD_ACCOUNT_IDS}")

    for aid in META_AD_ACCOUNT_IDS:
        print(f"\n── Account act_{aid} ──")

        print("  Fetching summary windows…")
        per_account_summary[aid] = {
            "today":        summarize(fetch_insights(aid, "account", d(0),  d(0))),
            "yesterday":    summarize(fetch_insights(aid, "account", d(1),  d(1))),
            "last_7_days":  summarize(fetch_insights(aid, "account", d(6),  d(0))),
            "last_30_days": summarize(fetch_insights(aid, "account", d(29), d(0))),
        }

        print("  Fetching per-ad daily breakdown (last 30d)…")
        ad_rows_raw = fetch_insights(aid, "ad", d(29), d(0), time_increment=1)
        ad_rows = [normalize_row(r) for r in ad_rows_raw]
        for r in ad_rows:
            r["account_id"] = aid
        print(f"    {len(ad_rows)} ad-day rows")
        combined_ads.extend(ad_rows)

        print("  Fetching per-adset 30d totals…")
        adset_rows = [normalize_row(r) for r in fetch_insights(aid, "adset", d(29), d(0))]
        for r in adset_rows:
            r["account_id"] = aid
        combined_adsets.extend(adset_rows)

        print("  Fetching per-campaign 30d totals…")
        campaign_rows = [normalize_row(r) for r in fetch_insights(aid, "campaign", d(29), d(0))]
        for r in campaign_rows:
            r["account_id"] = aid
        combined_campaigns.extend(campaign_rows)

        print("  Fetching on/off status (campaigns + adsets + ads)…")
        acct_statuses = {
            "campaigns": fetch_statuses(aid, "campaigns"),
            "adsets":    fetch_statuses(aid, "adsets"),
            "ads":       fetch_statuses(aid, "ads"),
        }
        combined_statuses[aid] = acct_statuses
        n_camp_active  = sum(1 for v in acct_statuses["campaigns"].values() if v["effective_status"] == "ACTIVE")
        n_adset_active = sum(1 for v in acct_statuses["adsets"].values()    if v["effective_status"] == "ACTIVE")
        n_ad_active    = sum(1 for v in acct_statuses["ads"].values()       if v["effective_status"] == "ACTIVE")
        print(f"    Active: {n_camp_active} campaigns, {n_adset_active} adsets, {n_ad_active} ads")

    # Blended totals — "All" view in the dashboard. Just add up the
    # already-computed per-account summary dicts. Rates (CTR, CPM, CPC,
    # frequency) get re-derived from the summed primitives so they stay
    # correct after aggregation.
    def _blend_summaries(dicts: list) -> dict:
        s = {"spend": 0.0, "impressions": 0, "clicks": 0, "reach": 0, "link_clicks": 0}
        actions: dict = {}
        freq_weighted = 0.0
        for d_ in dicts:
            s["spend"]       += d_.get("spend", 0.0)
            s["impressions"] += d_.get("impressions", 0)
            s["clicks"]      += d_.get("clicks", 0)
            s["reach"]       += d_.get("reach", 0)
            s["link_clicks"] += d_.get("link_clicks", 0)
            freq_weighted    += d_.get("frequency", 0.0) * d_.get("impressions", 0)
            for k, v in (d_.get("actions") or {}).items():
                actions[k] = actions.get(k, 0.0) + v
        s["actions"]              = actions
        s["cpm"]                  = (s["spend"] / s["impressions"] * 1000) if s["impressions"] else 0
        s["cpc"]                  = (s["spend"] / s["clicks"])              if s["clicks"]      else 0
        s["ctr"]                  = (s["clicks"] / s["impressions"] * 100)  if s["impressions"] else 0
        s["link_ctr"]             = (s["link_clicks"] / s["impressions"] * 100) if s["impressions"] else 0
        s["cost_per_link_click"]  = (s["spend"] / s["link_clicks"])         if s["link_clicks"] else 0
        s["frequency"]            = (freq_weighted / s["impressions"])      if s["impressions"] else 0
        return s

    blended_summary = {
        window: _blend_summaries([per_account_summary[aid][window] for aid in META_AD_ACCOUNT_IDS])
        for window in ("today", "yesterday", "last_7_days", "last_30_days")
    }

    # ── Attach budget/status to every row, keyed by ID ───────────────────
    # Ad-set NAMES are not unique: duplicating an ad set keeps its name, and in
    # this account 33 of 98 names map to several ids -- four different ad sets
    # are called "HM-5". Any consumer that looks a budget up by name gets
    # whichever copy it happens to find, usually a paused one with a stale
    # figure, and there is nothing in the output to reveal the mismatch.
    #
    # So the budget travels ON the row. A caller reading adsets/ads never has
    # to join, and therefore cannot join wrongly.
    def _decorate(rows, level_key):
        for row in rows:
            acct = str(row.get("account_id") or "")
            ent = ((combined_statuses.get(acct) or {}).get(level_key) or {})
            info = ent.get(str(row.get(f"{level_key[:-1]}_id") or "")) or {}
            if level_key == "ads":
                # An ad has no budget of its own; carry its ad set's, so the
                # per-day ad rows (the only ones with real history) are usable
                # on their own.
                aset = ((combined_statuses.get(acct) or {}).get("adsets") or {})
                info = aset.get(str(row.get("adset_id") or "")) or {}
            row["daily_budget"] = info.get("daily_budget")
            row["lifetime_budget"] = info.get("lifetime_budget")
            row["effective_status"] = info.get("effective_status")
        return rows

    _decorate(combined_adsets, "adsets")
    _decorate(combined_ads, "ads")
    _decorate(combined_campaigns, "campaigns")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # New multi-account fields — the dashboard should read these.
        "account_ids": META_AD_ACCOUNT_IDS,
        "summary": blended_summary,             # blended across all accounts (the "All" view)
        "summary_by_account": per_account_summary,  # per-account KPI cards
        "statuses_by_account": combined_statuses,   # { <account_id>: { campaigns:{}, adsets:{}, ads:{} } }
        # Row-level tables — every row has `account_id`, dashboard filters.
        "campaigns": combined_campaigns,
        "adsets":    combined_adsets,
        "ads":       combined_ads,
        # Backward-compat fields for any consumer still reading the
        # single-account shape. Points at the first (primary) account so
        # existing dashboard code that hasn't been updated yet still
        # shows something sensible.
        "account_id": META_AD_ACCOUNT_ID,
        "statuses": combined_statuses.get(META_AD_ACCOUNT_ID, {}),
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"  Wrote {OUTPUT_FILE} ({os.path.getsize(OUTPUT_FILE):,} bytes)")

    publish_output(OUTPUT_FILE, OUTPUT_FILE)
    print("✅ Done")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ Fatal: {e}", file=sys.stderr)
        sys.exit(1)
