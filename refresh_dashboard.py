#!/usr/bin/env python3
"""
Dashboard Data Refresher.

Runs hourly via GitHub Actions.

1. Reads ASA access token from Google Sheet (_Config!B1)
2. Fetches ASA data: campaigns, ad groups, keywords, ads — all with performance metrics
3. Fetches RevenueCat data: subscribers with attribution + revenue
4. Matches spend to revenue per keyword/campaign
5. Writes everything to multiple Google Sheet tabs
"""

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone


SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
REVENUECAT_API_KEY = os.environ["REVENUECAT_API_KEY"]
REVENUECAT_PROJECT_ID = os.environ.get("REVENUECAT_PROJECT_ID", "6afc72a9")
ORG_ID = os.environ.get("ASA_ORG_ID", "8868820")


# ══════════════════════════════════════════════════════════════════
# Google Sheets helpers
# ══════════════════════════════════════════════════════════════════

def base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def get_google_access_token() -> str:
    creds = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(creds["private_key"])
        key_path = f.name

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iss": creds["client_email"],
        "scope": "https://www.googleapis.com/auth/spreadsheets",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }
    h = base64url_encode(json.dumps(header, separators=(",", ":")).encode())
    p = base64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode("ascii")
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_path],
        input=signing_input, capture_output=True, check=True,
    )
    os.unlink(key_path)
    jwt_token = f"{h}.{p}.{base64url_encode(result.stdout)}"

    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_token,
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())["access_token"]


def sheets_read(google_token: str, range_: str) -> list:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{urllib.parse.quote(range_)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {google_token}"})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            return data.get("values", [])
    except urllib.error.HTTPError:
        return []


def sheets_write(google_token: str, range_: str, values: list) -> None:
    # Clear first
    clear_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{urllib.parse.quote(range_)}:clear"
    req = urllib.request.Request(
        clear_url, data=b"{}",
        headers={"Authorization": f"Bearer {google_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        print(f"Clear warning: {e.code} for {range_}")

    # Write
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/"
        f"{urllib.parse.quote(range_)}?valueInputOption=USER_ENTERED"
    )
    body = json.dumps({"range": range_, "values": values}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {google_token}", "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()


def sheets_ensure_tab(google_token: str, tab_name: str) -> None:
    """Create tab if doesn't exist."""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {google_token}"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())
    tabs = {s["properties"]["title"] for s in data.get("sheets", [])}
    if tab_name in tabs:
        return
    batch_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}:batchUpdate"
    body = json.dumps({
        "requests": [{"addSheet": {"properties": {"title": tab_name}}}]
    }).encode()
    req = urllib.request.Request(
        batch_url, data=body,
        headers={"Authorization": f"Bearer {google_token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()


# ══════════════════════════════════════════════════════════════════
# Apple Search Ads API
# ══════════════════════════════════════════════════════════════════

def asa_request(asa_token: str, method: str, path: str, body: dict = None) -> dict:
    url = f"https://api.searchads.apple.com/api/v5{path}"
    headers = {
        "Authorization": f"Bearer {asa_token}",
        "X-AP-Context": f"orgId={ORG_ID}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"ASA API error: {method} {path} -> {e.code}")
        print(f"Response: {e.read().decode()[:500]}")
        raise


def asa_get_campaigns(asa_token: str) -> list:
    all_campaigns = []
    offset = 0
    while True:
        data = asa_request(asa_token, "GET", f"/campaigns?limit=1000&offset={offset}")
        campaigns = data.get("data", [])
        all_campaigns.extend(campaigns)
        if len(campaigns) < 1000:
            break
        offset += 1000
    return all_campaigns


def asa_report(asa_token: str, report_type: str, start_date: str, end_date: str,
               campaign_id: str = None, granularity: str = None) -> list:
    """
    Get ASA report data.
    report_type: 'campaigns', 'adgroups', 'keywords', 'searchterms', 'ads'
    """
    if report_type == "campaigns":
        path = "/reports/campaigns"
        body = {
            "startTime": start_date,
            "endTime": end_date,
            "selector": {
                "orderBy": [{"field": "localSpend", "sortOrder": "DESCENDING"}],
                "pagination": {"offset": 0, "limit": 1000},
            },
            "timeZone": "UTC",
            "returnRecordsWithNoMetrics": False,
            "returnRowTotals": True,
            "returnGrandTotals": False,
        }
    elif report_type == "adgroups":
        path = f"/reports/campaigns/{campaign_id}/adgroups"
        body = {
            "startTime": start_date,
            "endTime": end_date,
            "selector": {
                "orderBy": [{"field": "localSpend", "sortOrder": "DESCENDING"}],
                "pagination": {"offset": 0, "limit": 1000},
            },
            "timeZone": "UTC",
            "returnRecordsWithNoMetrics": True,
            "returnRowTotals": True,
        }
    elif report_type == "keywords":
        path = f"/reports/campaigns/{campaign_id}/keywords"
        body = {
            "startTime": start_date,
            "endTime": end_date,
            "selector": {
                "orderBy": [{"field": "localSpend", "sortOrder": "DESCENDING"}],
                "pagination": {"offset": 0, "limit": 1000},
            },
            "timeZone": "UTC",
            "returnRecordsWithNoMetrics": True,
            "returnRowTotals": True,
        }
    elif report_type == "ads":
        path = f"/reports/campaigns/{campaign_id}/ads"
        body = {
            "startTime": start_date,
            "endTime": end_date,
            "selector": {
                "orderBy": [{"field": "localSpend", "sortOrder": "DESCENDING"}],
                "pagination": {"offset": 0, "limit": 1000},
            },
            "timeZone": "UTC",
            "returnRecordsWithNoMetrics": True,
            "returnRowTotals": True,
        }
    else:
        raise ValueError(f"Unknown report type: {report_type}")

    try:
        resp = asa_request(asa_token, "POST", path, body)
        return resp.get("data", {}).get("reportingDataResponse", {}).get("row", [])
    except urllib.error.HTTPError as e:
        print(f"Report failed for campaign {campaign_id}: {e}")
        return []


# ══════════════════════════════════════════════════════════════════
# RevenueCat API — pull all subscribers with attribution
# ══════════════════════════════════════════════════════════════════

def rc_get_all_customers() -> list:
    """
    Get all subscribers with their attribution + revenue.
    Uses the RevenueCat v2 API.
    """
    all_customers = []
    starting_after = None
    page = 0
    while True:
        page += 1
        url = f"https://api.revenuecat.com/v2/projects/{REVENUECAT_PROJECT_ID}/customers?limit=1000"
        if starting_after:
            url += f"&starting_after={starting_after}"

        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {REVENUECAT_API_KEY}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            print(f"RC API error: {e.code} - {e.read().decode()[:200]}")
            break

        items = data.get("items", [])
        all_customers.extend(items)
        print(f"  RC page {page}: fetched {len(items)} customers (total: {len(all_customers)})")

        next_page = data.get("next_page")
        if not next_page:
            break
        # Parse starting_after from next_page URL
        if "starting_after=" in next_page:
            starting_after = next_page.split("starting_after=")[1].split("&")[0]
        else:
            break

    return all_customers


# ══════════════════════════════════════════════════════════════════
# Data aggregation and matching
# ══════════════════════════════════════════════════════════════════

def build_revenue_index(customers: list) -> dict:
    """
    Build index of revenue by (campaign, keyword, country) and date ranges.
    Returns dict with different time ranges.
    """
    now = datetime.now(timezone.utc)
    ranges = {
        "7d": now - timedelta(days=7),
        "14d": now - timedelta(days=14),
        "30d": now - timedelta(days=30),
        "all": None,
    }

    # Flat index: (campaign, keyword) -> {range: {users, revenue, active}}
    by_keyword = defaultdict(lambda: defaultdict(lambda: {"users": 0, "revenue": 0.0, "active": 0}))
    by_campaign = defaultdict(lambda: defaultdict(lambda: {"users": 0, "revenue": 0.0, "active": 0}))
    by_country = defaultdict(lambda: defaultdict(lambda: {"users": 0, "revenue": 0.0, "active": 0}))

    for c in customers:
        # RevenueCat v2 API structure
        first_seen_ms = c.get("first_seen_at")
        if first_seen_ms:
            first_seen = datetime.fromtimestamp(first_seen_ms / 1000, tz=timezone.utc)
        else:
            continue

        # Attribution
        attrs = c.get("attributes", {})
        media_source = attrs.get("$mediaSource", {}).get("value", "") if isinstance(attrs.get("$mediaSource"), dict) else ""
        campaign = attrs.get("$campaign", {}).get("value", "") if isinstance(attrs.get("$campaign"), dict) else ""
        keyword = attrs.get("$keyword", {}).get("value", "") if isinstance(attrs.get("$keyword"), dict) else ""
        country = attrs.get("$ip_country_code", {}).get("value", "") if isinstance(attrs.get("$ip_country_code"), dict) else ""

        if media_source != "Apple Search Ads":
            continue

        # Revenue
        total_spent = float(c.get("total_spent_in_usd", 0) or 0)
        is_active = 1 if c.get("active_entitlements") else 0

        for range_name, range_start in ranges.items():
            if range_start and first_seen < range_start:
                continue
            by_keyword[(campaign, keyword.lower())][range_name]["users"] += 1
            by_keyword[(campaign, keyword.lower())][range_name]["revenue"] += total_spent
            by_keyword[(campaign, keyword.lower())][range_name]["active"] += is_active

            by_campaign[campaign][range_name]["users"] += 1
            by_campaign[campaign][range_name]["revenue"] += total_spent
            by_campaign[campaign][range_name]["active"] += is_active

            by_country[country][range_name]["users"] += 1
            by_country[country][range_name]["revenue"] += total_spent
            by_country[country][range_name]["active"] += is_active

    return {"by_keyword": by_keyword, "by_campaign": by_campaign, "by_country": by_country}


def get_country_from_campaign(campaign_name: str, campaign_countries: str) -> str:
    """Infer country from campaign name or ASA country list."""
    if campaign_countries:
        return campaign_countries.split(",")[0].strip()
    name_lower = campaign_name.lower()
    if "us " in name_lower or name_lower.startswith("us_") or "us —" in name_lower:
        return "US"
    if "uk " in name_lower or name_lower.startswith("uk_") or "uk —" in name_lower:
        return "GB"
    if "canada" in name_lower or name_lower.startswith("ca_"):
        return "CA"
    return ""


# ══════════════════════════════════════════════════════════════════
# Write tabs
# ══════════════════════════════════════════════════════════════════

def roas_color(roas: float) -> str:
    """Return color formula suggestion based on ROAS."""
    if roas >= 100:
        return "WIN"
    if roas >= 50:
        return "WATCH"
    if roas > 0:
        return "LOSS"
    return "DEAD"


def write_dashboard_tab(google_token: str, asa_data_all, rev_index: dict) -> None:
    """Top-level summary by country."""
    headers = ["Country", "Spend 7d", "Revenue 7d", "ROAS 7d", "Spend 14d", "Revenue 14d", "ROAS 14d",
               "Spend 30d", "Revenue 30d", "ROAS 30d", "Spend all", "Revenue all", "ROAS all"]
    rows = [headers]

    # Aggregate by country
    country_spend = defaultdict(lambda: defaultdict(float))
    for range_name, campaigns in asa_data_all.items():
        for c in campaigns:
            country = c.get("_country", "?")
            country_spend[country][range_name] += c.get("spend", 0)

    for country in sorted(country_spend.keys(), key=lambda x: -country_spend[x].get("30d", 0)):
        row = [country]
        for r in ["7d", "14d", "30d", "all"]:
            spend = country_spend[country].get(r, 0)
            rev = rev_index["by_country"].get(country, {}).get(r, {}).get("revenue", 0)
            roas = (rev / spend * 100) if spend > 0 else 0
            row.extend([f"${spend:.2f}", f"${rev:.2f}", f"{roas:.0f}%"])
        rows.append(row)

    # Timestamp
    rows.append([""])
    rows.append([f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"])

    sheets_write(google_token, "Dashboard!A1:M100", rows)


def write_campaigns_tab(google_token: str, asa_data_all, rev_index: dict) -> None:
    headers = ["Campaign", "Country", "Spend 7d", "Revenue 7d", "Subs 7d", "ROAS 7d",
               "Spend 30d", "Revenue 30d", "Subs 30d", "ROAS 30d",
               "Spend all", "Revenue all", "Subs all", "ROAS all", "Status"]
    rows = [headers]

    # Unique campaigns — use 30d as reference for row presence
    all_camp_names = set()
    for campaigns in asa_data_all.values():
        for c in campaigns:
            all_camp_names.add(c.get("_name", ""))

    campaign_rows = []
    for name in all_camp_names:
        row_data = {"name": name}
        for r in ["7d", "14d", "30d", "all"]:
            camp = next((c for c in asa_data_all[r] if c.get("_name") == name), None)
            row_data[f"spend_{r}"] = camp.get("spend", 0) if camp else 0
            row_data[f"country"] = camp.get("_country", "") if camp else ""
            rev_data = rev_index["by_campaign"].get(name, {}).get(r, {"users": 0, "revenue": 0})
            row_data[f"rev_{r}"] = rev_data["revenue"]
            row_data[f"subs_{r}"] = rev_data["users"]

        campaign_rows.append(row_data)

    campaign_rows.sort(key=lambda x: -x["spend_30d"])

    for cr in campaign_rows:
        row = [cr["name"], cr["country"]]
        for r in ["7d", "30d", "all"]:
            spend = cr[f"spend_{r}"]
            rev = cr[f"rev_{r}"]
            subs = cr[f"subs_{r}"]
            roas = (rev / spend * 100) if spend > 0 else 0
            row.extend([f"${spend:.2f}", f"${rev:.2f}", subs, f"{roas:.0f}%"])
        # Status based on 30d ROAS
        roas_30 = (cr["rev_30d"] / cr["spend_30d"] * 100) if cr["spend_30d"] > 0 else 0
        row.append(roas_color(roas_30))
        rows.append(row)

    sheets_write(google_token, "Campaigns!A1:O1000", rows)


def write_keywords_tab(google_token: str, asa_kw_data_all: dict, rev_index: dict) -> None:
    headers = ["Keyword", "Campaign", "Country", "Match", "Status",
               "Spend 7d", "Revenue 7d", "Subs 7d", "Installs 7d", "ROAS 7d", "CPA 7d",
               "Spend 30d", "Revenue 30d", "Subs 30d", "Installs 30d", "ROAS 30d", "CPA 30d",
               "Spend all", "Revenue all", "Subs all", "ROAS all", "CPA all",
               "Avg CPT", "TTR", "CR", "IS", "Flag"]
    rows = [headers]

    # Get all unique keyword/campaign combos
    all_kws = set()
    for kws in asa_kw_data_all.values():
        for k in kws:
            all_kws.add((k.get("_campaign", ""), k.get("keyword", "").lower()))

    kw_rows = []
    for camp_name, kw in all_kws:
        row_data = {"kw": kw, "camp": camp_name}
        for r in ["7d", "14d", "30d", "all"]:
            kw_obj = next((k for k in asa_kw_data_all[r]
                          if k.get("_campaign") == camp_name and k.get("keyword", "").lower() == kw), None)
            row_data[f"spend_{r}"] = kw_obj.get("spend", 0) if kw_obj else 0
            row_data[f"inst_{r}"] = kw_obj.get("installs", 0) if kw_obj else 0
            row_data[f"taps_{r}"] = kw_obj.get("taps", 0) if kw_obj else 0
            row_data[f"imp_{r}"] = kw_obj.get("impressions", 0) if kw_obj else 0
            row_data[f"cpt_{r}"] = kw_obj.get("cpt", 0) if kw_obj else 0
            if kw_obj:
                row_data["country"] = kw_obj.get("_country", "")
                row_data["match"] = kw_obj.get("match_type", "")
                row_data["kw_status"] = kw_obj.get("kw_status", "")
                row_data["is"] = kw_obj.get("impression_share", "")
            rev_data = rev_index["by_keyword"].get((camp_name, kw), {}).get(r, {"users": 0, "revenue": 0})
            row_data[f"rev_{r}"] = rev_data["revenue"]
            row_data[f"subs_{r}"] = rev_data["users"]

        kw_rows.append(row_data)

    kw_rows.sort(key=lambda x: -x.get("spend_30d", 0))

    for k in kw_rows:
        row = [k["kw"], k["camp"], k.get("country", ""), k.get("match", ""), k.get("kw_status", "")]
        for r in ["7d", "30d", "all"]:
            spend = k[f"spend_{r}"]
            rev = k[f"rev_{r}"]
            subs = k[f"subs_{r}"]
            inst = k[f"inst_{r}"]
            roas = (rev / spend * 100) if spend > 0 else 0
            cpa = (spend / inst) if inst > 0 else 0
            row.extend([f"${spend:.2f}", f"${rev:.2f}", subs, inst,
                       f"{roas:.0f}%", f"${cpa:.2f}"])

        # CPT/TTR/CR from 30d data
        imp30 = k.get("imp_30d", 0)
        taps30 = k.get("taps_30d", 0)
        inst30 = k.get("inst_30d", 0)
        cpt30 = k.get("cpt_30d", 0)
        ttr = (taps30 / imp30 * 100) if imp30 > 0 else 0
        cr = (inst30 / taps30 * 100) if taps30 > 0 else 0
        row.extend([f"${cpt30:.2f}", f"{ttr:.1f}%", f"{cr:.0f}%", k.get("is", "")])

        # Flag
        spend30 = k["spend_30d"]
        rev30 = k["rev_30d"]
        roas30 = (rev30 / spend30 * 100) if spend30 > 0 else 0
        if spend30 < 15:
            flag = "WAIT"
        elif rev30 == 0:
            flag = "PAUSE — 0 revenue"
        elif roas30 < 30:
            flag = "LOSING"
        elif roas30 > 100:
            flag = "WINNER"
        elif roas30 > 50:
            flag = "OK"
        else:
            flag = "WATCH"
        row.append(flag)

        rows.append(row)

    sheets_write(google_token, "Keywords!A1:AA5000", rows)


def write_winners_losers_tabs(google_token: str, rev_index: dict, asa_kw_data_30d: list) -> None:
    """Winners: ROAS > 100%. Losers: $15+ spend, <30% ROAS."""
    win_headers = ["Keyword", "Campaign", "Country", "Spend 30d", "Revenue 30d", "Subs", "ROAS", "CPA"]
    lose_headers = win_headers
    winners = [win_headers]
    losers = [lose_headers]

    for k in sorted(asa_kw_data_30d, key=lambda x: -x.get("spend", 0)):
        kw = k.get("keyword", "").lower()
        camp = k.get("_campaign", "")
        spend = k.get("spend", 0)
        inst = k.get("installs", 0)
        if spend < 15:
            continue

        rev_data = rev_index["by_keyword"].get((camp, kw), {}).get("30d", {"users": 0, "revenue": 0})
        rev = rev_data["revenue"]
        subs = rev_data["users"]
        roas = (rev / spend * 100) if spend > 0 else 0
        cpa = (spend / inst) if inst > 0 else 0

        row = [kw, camp, k.get("_country", ""),
               f"${spend:.2f}", f"${rev:.2f}", subs,
               f"{roas:.0f}%", f"${cpa:.2f}"]

        if roas >= 100:
            winners.append(row)
        elif roas < 30:
            losers.append(row)

    sheets_write(google_token, "Winners!A1:H500", winners)
    sheets_write(google_token, "Losers!A1:H500", losers)


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

def main() -> None:
    google_token = get_google_access_token()

    # Ensure all tabs exist
    for tab in ["Dashboard", "Campaigns", "Keywords", "Winners", "Losers", "Ads", "_Config"]:
        sheets_ensure_tab(google_token, tab)

    # Get ASA token from sheet
    config = sheets_read(google_token, "_Config!B1:B1")
    if not config or not config[0]:
        print("❌ No ASA token in _Config!B1. Run refresh_token.py first.")
        return
    asa_token = config[0][0]
    print(f"Got ASA token: {asa_token[:40]}...")

    # Get campaigns
    print("\nFetching campaigns...")
    campaigns = asa_get_campaigns(asa_token)
    print(f"Found {len(campaigns)} campaigns")

    # Build campaign metadata lookup
    campaign_meta = {}
    for c in campaigns:
        cid = c["id"]
        campaign_meta[cid] = {
            "id": cid,
            "name": c.get("name", ""),
            "status": c.get("status", ""),
            "countries": ",".join(c.get("countriesOrRegions", [])),
            "budget": c.get("dailyBudgetAmount", {}).get("amount", ""),
        }

    # Date ranges
    today = datetime.now(timezone.utc).date()
    ranges = {
        "7d": (today - timedelta(days=7), today),
        "14d": (today - timedelta(days=14), today),
        "30d": (today - timedelta(days=30), today),
        "all": (datetime(2026, 3, 1).date(), today),  # campaign start around March
    }

    # Fetch campaign-level reports for all ranges
    asa_campaign_data = {r: [] for r in ranges}
    asa_keyword_data = {r: [] for r in ranges}
    asa_ad_data = {r: [] for r in ranges}

    for range_name, (start, end) in ranges.items():
        print(f"\n--- Fetching {range_name} ({start} to {end}) ---")
        start_str = start.isoformat()
        end_str = end.isoformat()

        # Campaign report
        camp_rows = asa_report(asa_token, "campaigns", start_str, end_str)
        for row in camp_rows:
            meta = row.get("metadata", {})
            total = row.get("total", {})
            cid = meta.get("campaignId")
            campaign_name = campaign_meta.get(cid, {}).get("name", meta.get("campaignName", ""))
            countries = campaign_meta.get(cid, {}).get("countries", "")
            asa_campaign_data[range_name].append({
                "_id": cid,
                "_name": campaign_name,
                "_country": get_country_from_campaign(campaign_name, countries),
                "spend": float(total.get("localSpend", {}).get("amount", 0) or 0),
                "impressions": int(total.get("impressions", 0) or 0),
                "taps": int(total.get("taps", 0) or 0),
                "installs": int(total.get("installs", 0) or 0),
                "cpt": float(total.get("avgCPT", {}).get("amount", 0) or 0),
                "cpa": float(total.get("avgCPA", {}).get("amount", 0) or 0),
                "ttr": float(total.get("ttr", 0) or 0),
            })

        # Keyword reports per campaign (only for active campaigns to save API calls)
        for cid, meta in campaign_meta.items():
            if meta["status"] != "ENABLED":
                continue
            try:
                kw_rows = asa_report(asa_token, "keywords", start_str, end_str, campaign_id=cid)
                for row in kw_rows:
                    m = row.get("metadata", {})
                    t = row.get("total", {})
                    asa_keyword_data[range_name].append({
                        "_campaign": meta["name"],
                        "_campaign_id": cid,
                        "_country": get_country_from_campaign(meta["name"], meta["countries"]),
                        "keyword": m.get("keyword", ""),
                        "keyword_id": m.get("keywordId"),
                        "match_type": m.get("matchType", ""),
                        "kw_status": m.get("keywordStatus", ""),
                        "bid": float(m.get("bidAmount", {}).get("amount", 0) or 0) if m.get("bidAmount") else 0,
                        "impression_share": m.get("impressionShare", ""),
                        "spend": float(t.get("localSpend", {}).get("amount", 0) or 0),
                        "impressions": int(t.get("impressions", 0) or 0),
                        "taps": int(t.get("taps", 0) or 0),
                        "installs": int(t.get("installs", 0) or 0),
                        "cpt": float(t.get("avgCPT", {}).get("amount", 0) or 0),
                    })
            except Exception as e:
                print(f"  Skipped keyword report for {meta['name']}: {e}")

            # Ads report per campaign
            if range_name == "30d":  # only for 30d to save time
                try:
                    ad_rows = asa_report(asa_token, "ads", start_str, end_str, campaign_id=cid)
                    for row in ad_rows:
                        m = row.get("metadata", {})
                        t = row.get("total", {})
                        asa_ad_data[range_name].append({
                            "_campaign": meta["name"],
                            "_country": get_country_from_campaign(meta["name"], meta["countries"]),
                            "ad_name": m.get("adName", m.get("name", "")),
                            "cpp_id": m.get("cppId", ""),
                            "spend": float(t.get("localSpend", {}).get("amount", 0) or 0),
                            "installs": int(t.get("installs", 0) or 0),
                            "impressions": int(t.get("impressions", 0) or 0),
                            "taps": int(t.get("taps", 0) or 0),
                        })
                except Exception as e:
                    print(f"  Skipped ad report for {meta['name']}: {e}")

        print(f"  Keywords: {len(asa_keyword_data[range_name])}")
        print(f"  Campaigns: {len(asa_campaign_data[range_name])}")

    # Fetch RevenueCat data
    print("\n--- Fetching RevenueCat customers ---")
    customers = rc_get_all_customers()
    print(f"Total RC customers: {len(customers)}")

    rev_index = build_revenue_index(customers)

    # Write all tabs
    print("\n--- Writing tabs ---")
    write_dashboard_tab(google_token, asa_campaign_data, rev_index)
    print("  ✅ Dashboard")
    write_campaigns_tab(google_token, asa_campaign_data, rev_index)
    print("  ✅ Campaigns")
    write_keywords_tab(google_token, asa_keyword_data, rev_index)
    print("  ✅ Keywords")
    write_winners_losers_tabs(google_token, rev_index, asa_keyword_data["30d"])
    print("  ✅ Winners + Losers")

    # Ads tab
    ad_headers = ["Ad", "Campaign", "Country", "CPP ID", "Spend", "Installs", "Impressions", "Taps", "CPA"]
    ad_rows = [ad_headers]
    for ad in sorted(asa_ad_data["30d"], key=lambda x: -x["spend"]):
        cpa = ad["spend"] / ad["installs"] if ad["installs"] > 0 else 0
        ad_rows.append([ad["ad_name"], ad["_campaign"], ad["_country"], ad["cpp_id"],
                       f"${ad['spend']:.2f}", ad["installs"], ad["impressions"],
                       ad["taps"], f"${cpa:.2f}"])
    sheets_write(google_token, "Ads!A1:I500", ad_rows)
    print("  ✅ Ads")

    # Write last update timestamp
    sheets_write(google_token, "_Config!B3",
                 [[datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")]])
    sheets_write(google_token, "_Config!A3", [["Last Dashboard Refresh"]])

    print("\n🎉 Dashboard refreshed!")


if __name__ == "__main__":
    main()
