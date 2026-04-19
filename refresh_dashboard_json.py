#!/usr/bin/env python3
"""
Dashboard Data Refresher — generates JSON + uploads to cPanel via FTP.

Runs hourly via GitHub Actions.

1. Reads ASA access token from Google Sheet (_Config!B1)
2. Fetches ASA data: campaigns, ad groups, keywords, ads
3. Fetches RevenueCat data
4. Matches spend to revenue
5. Outputs data.json
6. Uploads via FTP to the dashboard folder on cPanel
"""

import base64
import ftplib
import hashlib
import json
import os
import ssl
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

FTP_HOST = os.environ.get("FTP_HOST", "")
FTP_USER = os.environ.get("FTP_USER", "")
FTP_PASS = os.environ.get("FTP_PASS", "")
FTP_PATH = os.environ.get("FTP_PATH", "/public_html/ads-dashboard")


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
        print(f"ASA API error: {method} {path} -> {e.code}: {e.read().decode()[:200]}")
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


def asa_report(asa_token: str, report_type: str, start: str, end: str, campaign_id: str = None) -> list:
    if report_type == "campaigns":
        path = "/reports/campaigns"
        body = {
            "startTime": start,
            "endTime": end,
            "selector": {
                "orderBy": [{"field": "localSpend", "sortOrder": "DESCENDING"}],
                "pagination": {"offset": 0, "limit": 1000},
            },
            "timeZone": "UTC",
            "returnRecordsWithNoMetrics": False,
            "returnRowTotals": True,
            "returnGrandTotals": False,
        }
    elif report_type == "keywords":
        path = f"/reports/campaigns/{campaign_id}/keywords"
        body = {
            "startTime": start, "endTime": end,
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
            "startTime": start, "endTime": end,
            "selector": {
                "orderBy": [{"field": "localSpend", "sortOrder": "DESCENDING"}],
                "pagination": {"offset": 0, "limit": 1000},
            },
            "timeZone": "UTC",
            "returnRecordsWithNoMetrics": True,
            "returnRowTotals": True,
        }
    else:
        return []

    try:
        resp = asa_request(asa_token, "POST", path, body)
        return resp.get("data", {}).get("reportingDataResponse", {}).get("row", [])
    except Exception as e:
        print(f"  Report failed for {report_type} / {campaign_id}: {e}")
        return []


# ══════════════════════════════════════════════════════════════════
# RevenueCat
# ══════════════════════════════════════════════════════════════════

def rc_get_all_customers() -> list:
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
            print(f"RC API error: {e.code}")
            break
        items = data.get("items", [])
        all_customers.extend(items)
        next_page = data.get("next_page")
        if not next_page:
            break
        if "starting_after=" in next_page:
            starting_after = next_page.split("starting_after=")[1].split("&")[0]
        else:
            break
    print(f"  Total RC customers: {len(all_customers)}")
    return all_customers


def rc_fetch_customer_attrs(customer_id: str) -> dict:
    """Fetch attribution attributes for one customer."""
    encoded = urllib.parse.quote(customer_id, safe="")
    url = f"https://api.revenuecat.com/v2/projects/{REVENUECAT_PROJECT_ID}/customers/{encoded}/attributes"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {REVENUECAT_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        result = {}
        for item in data.get("items", []):
            result[item.get("name", "")] = item.get("value", "")
        return result
    except Exception:
        return {}


def rc_fetch_customer_purchases(customer_id: str) -> float:
    """Sum revenue in USD from all purchases for one customer."""
    encoded = urllib.parse.quote(customer_id, safe="")
    url = f"https://api.revenuecat.com/v2/projects/{REVENUECAT_PROJECT_ID}/customers/{encoded}/purchases?limit=100"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {REVENUECAT_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        total = 0.0
        for p in data.get("items", []):
            # Try multiple possible field names for amount
            for key in ["revenue_in_usd", "amount_in_usd", "price_in_usd", "revenue", "price"]:
                v = p.get(key)
                if v is not None:
                    try:
                        total += float(v)
                        break
                    except (TypeError, ValueError):
                        pass
        return total
    except Exception:
        return 0.0


def rc_fetch_customer_active(customer_id: str) -> bool:
    """Check if customer has any active entitlement."""
    encoded = urllib.parse.quote(customer_id, safe="")
    url = f"https://api.revenuecat.com/v2/projects/{REVENUECAT_PROJECT_ID}/customers/{encoded}/active_entitlements"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {REVENUECAT_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return len(data.get("items", [])) > 0
    except Exception:
        return False


def rc_enrich_customers(customers: list) -> list:
    """
    For each customer, fetch their attributes + purchases in parallel.
    Skip customers from before our earliest date range (March 1, 2026).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Cutoff — only enrich customers from after this date to save API calls
    cutoff_ms = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp() * 1000)

    to_enrich = [c for c in customers if c.get("first_seen_at", 0) >= cutoff_ms]
    print(f"  Enriching {len(to_enrich)} customers (from {len(customers)} total)...")

    def enrich_one(customer):
        cid = customer["id"]
        attrs = rc_fetch_customer_attrs(cid)
        customer["_attrs"] = attrs
        # Only fetch purchases + active if ASA-attributed (saves calls)
        if attrs.get("$mediaSource") == "Apple Search Ads":
            customer["_revenue"] = rc_fetch_customer_purchases(cid)
            customer["_active"] = rc_fetch_customer_active(cid)
        else:
            customer["_revenue"] = 0.0
            customer["_active"] = False
        return customer

    enriched = []
    done = 0
    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = [executor.submit(enrich_one, c) for c in to_enrich]
        for future in as_completed(futures):
            enriched.append(future.result())
            done += 1
            if done % 100 == 0:
                print(f"    {done}/{len(to_enrich)}")

    asa_count = sum(1 for c in enriched if c.get("_attrs", {}).get("$mediaSource") == "Apple Search Ads")
    print(f"  Enriched: {len(enriched)} total, {asa_count} ASA-attributed")
    return enriched


# ══════════════════════════════════════════════════════════════════
# Data processing
# ══════════════════════════════════════════════════════════════════

def get_country_from_campaign(name: str, countries: str) -> str:
    if countries:
        return countries.split(",")[0].strip()
    n = name.lower()
    if "us " in n or n.startswith("us_") or "us —" in n: return "US"
    if "uk " in n or n.startswith("uk_") or "uk —" in n: return "GB"
    if "canada" in n or n.startswith("ca_"): return "CA"
    return ""


def build_revenue_index(customers: list) -> dict:
    """Customers are expected to have been enriched with _attrs / _revenue / _active."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)

    ranges = {
        "today": today_start,
        "yesterday": yesterday_start,
        "7d": now - timedelta(days=7),
        "14d": now - timedelta(days=14),
        "30d": now - timedelta(days=30),
        "all": None,
    }

    by_kw = defaultdict(lambda: defaultdict(lambda: {"users": 0, "revenue": 0.0, "active": 0}))
    by_camp = defaultdict(lambda: defaultdict(lambda: {"users": 0, "revenue": 0.0, "active": 0}))
    by_country = defaultdict(lambda: defaultdict(lambda: {"users": 0, "revenue": 0.0, "active": 0}))

    for c in customers:
        first_seen_ms = c.get("first_seen_at")
        if not first_seen_ms:
            continue
        first_seen = datetime.fromtimestamp(first_seen_ms / 1000, tz=timezone.utc)

        attrs = c.get("_attrs", {})
        media_source = attrs.get("$mediaSource", "")
        if media_source != "Apple Search Ads":
            continue

        campaign = attrs.get("$campaign", "")
        keyword = attrs.get("$keyword", "").lower()
        country = c.get("last_seen_country", "")

        total = float(c.get("_revenue", 0) or 0)
        is_active = 1 if c.get("_active") else 0

        for r, start in ranges.items():
            if r == "yesterday":
                if not (yesterday_start <= first_seen < today_start):
                    continue
            elif start and first_seen < start:
                continue

            by_kw[(campaign, keyword)][r]["users"] += 1
            by_kw[(campaign, keyword)][r]["revenue"] += total
            by_kw[(campaign, keyword)][r]["active"] += is_active

            by_camp[campaign][r]["users"] += 1
            by_camp[campaign][r]["revenue"] += total
            by_camp[campaign][r]["active"] += is_active

            by_country[country][r]["users"] += 1
            by_country[country][r]["revenue"] += total

    return {"by_keyword": by_kw, "by_campaign": by_camp, "by_country": by_country}


# ══════════════════════════════════════════════════════════════════
# FTP upload
# ══════════════════════════════════════════════════════════════════

def upload_to_ftp(local_file: str, remote_name: str) -> None:
    if not FTP_HOST:
        print("  [skip FTP — no credentials]")
        return

    print(f"  Uploading to {FTP_HOST}:{FTP_PATH}/{remote_name}")
    try:
        # Use FTP_TLS for explicit FTPS
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        ftp = ftplib.FTP_TLS(FTP_HOST, timeout=60, context=ctx)
        ftp.login(FTP_USER, FTP_PASS)
        ftp.prot_p()

        # Navigate to directory
        try:
            ftp.cwd(FTP_PATH)
        except ftplib.error_perm:
            # Try to create it
            parts = FTP_PATH.strip("/").split("/")
            current = ""
            for part in parts:
                current = f"{current}/{part}" if current else f"/{part}"
                try:
                    ftp.cwd(current)
                except ftplib.error_perm:
                    try:
                        ftp.mkd(current)
                        ftp.cwd(current)
                    except Exception as e:
                        print(f"    Could not create {current}: {e}")

        with open(local_file, "rb") as f:
            ftp.storbinary(f"STOR {remote_name}", f)
        ftp.quit()
        print(f"    ✅ Uploaded {remote_name}")
    except Exception as e:
        print(f"    ❌ FTP upload failed: {e}")
        raise


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

def main() -> None:
    google_token = get_google_access_token()

    config = sheets_read(google_token, "_Config!B1:B1")
    if not config or not config[0]:
        print("❌ No ASA token in _Config!B1")
        return
    asa_token = config[0][0]

    print("Fetching campaigns...")
    campaigns = asa_get_campaigns(asa_token)
    print(f"Found {len(campaigns)} campaigns")

    campaign_meta = {}
    for c in campaigns:
        cid = c["id"]
        campaign_meta[cid] = {
            "id": cid,
            "name": c.get("name", ""),
            "status": c.get("status", ""),
            "countries": ",".join(c.get("countriesOrRegions", [])),
            "budget": c.get("dailyBudgetAmount", {}).get("amount", "") if c.get("dailyBudgetAmount") else "",
        }

    today = datetime.now(timezone.utc).date()
    ranges = {
        "today": (today, today),
        "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
        "7d": (today - timedelta(days=7), today),
        "14d": (today - timedelta(days=14), today),
        "30d": (today - timedelta(days=30), today),
        "all": (datetime(2026, 3, 1).date(), today),
    }

    asa_campaign_data = {r: {} for r in ranges}  # keyed by campaign_id
    asa_keyword_data = {r: {} for r in ranges}   # keyed by (campaign_id, keyword)
    asa_ad_data = {}  # for "all"

    for range_name, (start, end) in ranges.items():
        print(f"\n--- Fetching {range_name} ({start} to {end}) ---")
        start_s = start.isoformat()
        end_s = end.isoformat()

        # Campaign report
        try:
            rows = asa_report(asa_token, "campaigns", start_s, end_s)
            for row in rows:
                m = row.get("metadata", {})
                t = row.get("total", {})
                cid = m.get("campaignId")
                asa_campaign_data[range_name][cid] = {
                    "spend": float(t.get("localSpend", {}).get("amount", 0) or 0),
                    "impressions": int(t.get("impressions", 0) or 0),
                    "taps": int(t.get("taps", 0) or 0),
                    "installs": int(t.get("totalInstalls", 0) or 0),
                    "cpt": float(t.get("avgCPT", {}).get("amount", 0) or 0),
                    "cpa": float(t.get("totalAvgCPI", {}).get("amount", 0) or 0),
                    "ttr": float(t.get("ttr", 0) or 0),
                }
        except Exception as e:
            print(f"  Campaign report failed: {e}")

        # Keyword + Ad reports per campaign
        for cid, meta in campaign_meta.items():
            if meta["status"] != "ENABLED":
                continue

            kw_rows = asa_report(asa_token, "keywords", start_s, end_s, campaign_id=cid)
            for row in kw_rows:
                m = row.get("metadata", {})
                t = row.get("total", {})
                key = (cid, m.get("keyword", "").lower())
                asa_keyword_data[range_name][key] = {
                    "keyword": m.get("keyword", ""),
                    "campaign_id": cid,
                    "keyword_id": m.get("keywordId"),
                    "match": m.get("matchType", ""),
                    "status": m.get("keywordStatus", ""),
                    "bid": float(m.get("bidAmount", {}).get("amount", 0) or 0) if m.get("bidAmount") else 0,
                    "is": m.get("impressionShare", "") or "",
                    "spend": float(t.get("localSpend", {}).get("amount", 0) or 0),
                    "impressions": int(t.get("impressions", 0) or 0),
                    "taps": int(t.get("taps", 0) or 0),
                    "installs": int(t.get("totalInstalls", 0) or 0),
                    "cpt": float(t.get("avgCPT", {}).get("amount", 0) or 0),
                }

            # Only fetch ads for "all" to save time
            if range_name == "all":
                ad_rows = asa_report(asa_token, "ads", start_s, end_s, campaign_id=cid)
                for row in ad_rows:
                    m = row.get("metadata", {})
                    t = row.get("total", {})
                    ad_name = m.get("adName") or m.get("name") or "—"
                    asa_ad_data[(cid, ad_name)] = {
                        "name": ad_name,
                        "campaign_id": cid,
                        "campaign": meta["name"],
                        "country": get_country_from_campaign(meta["name"], meta["countries"]),
                        "cpp_id": m.get("cppId", ""),
                        "spend": float(t.get("localSpend", {}).get("amount", 0) or 0),
                        "impressions": int(t.get("impressions", 0) or 0),
                        "taps": int(t.get("taps", 0) or 0),
                        "installs": int(t.get("totalInstalls", 0) or 0),
                    }

        print(f"  Keywords this range: {len(asa_keyword_data[range_name])}")

    # Fetch RevenueCat
    print("\n--- Fetching RevenueCat ---")
    customers = rc_get_all_customers()
    customers = rc_enrich_customers(customers)
    rev_index = build_revenue_index(customers)

    # Build unified output
    print("\nBuilding JSON...")

    # Campaigns list
    campaigns_out = []
    for cid, meta in campaign_meta.items():
        country = get_country_from_campaign(meta["name"], meta["countries"])
        row = {
            "id": cid,
            "name": meta["name"],
            "status": meta["status"],
            "country": country,
            "budget": meta["budget"],
        }
        for r in ranges:
            d = asa_campaign_data[r].get(cid, {"spend": 0, "impressions": 0, "taps": 0, "installs": 0})
            row[f"spend_{r}"] = round(d["spend"], 2)
            row[f"installs_{r}"] = d["installs"]
            row[f"taps_{r}"] = d["taps"]
            row[f"impressions_{r}"] = d["impressions"]

            rev_data = rev_index["by_campaign"].get(meta["name"], {}).get(r, {"users": 0, "revenue": 0, "active": 0})
            row[f"revenue_{r}"] = round(rev_data["revenue"], 2)
            row[f"subs_{r}"] = rev_data["users"]
            row[f"active_{r}"] = rev_data["active"]
        campaigns_out.append(row)

    # Keywords list — merge across ranges by (campaign_id, keyword)
    kw_union = set()
    for r_data in asa_keyword_data.values():
        for key in r_data:
            kw_union.add(key)

    keywords_out = []
    for (cid, kw_lower) in kw_union:
        meta = campaign_meta.get(cid, {})
        country = get_country_from_campaign(meta.get("name", ""), meta.get("countries", ""))

        # Find any range that has the keyword's readable form/match/bid
        kw_display = kw_lower
        match = ""
        status = ""
        bid = 0
        is_share = ""
        for r in ["all", "30d", "14d", "7d", "today", "yesterday"]:
            d = asa_keyword_data[r].get((cid, kw_lower))
            if d:
                kw_display = d["keyword"]
                match = d.get("match") or match
                status = d.get("status") or status
                bid = d.get("bid") or bid
                is_share = d.get("is") or is_share
                break

        row = {
            "keyword": kw_display,
            "campaign_id": cid,
            "campaign": meta.get("name", ""),
            "country": country,
            "match": match,
            "status": status,
            "bid": bid,
            "impression_share": is_share,
        }
        for r in ranges:
            d = asa_keyword_data[r].get((cid, kw_lower), {"spend": 0, "impressions": 0, "taps": 0, "installs": 0, "cpt": 0})
            row[f"spend_{r}"] = round(d["spend"], 2)
            row[f"installs_{r}"] = d["installs"]
            row[f"taps_{r}"] = d["taps"]
            row[f"impressions_{r}"] = d["impressions"]
            row[f"cpt_{r}"] = round(d.get("cpt", 0), 2)

            rev_data = rev_index["by_keyword"].get((meta.get("name", ""), kw_lower), {}).get(r, {"users": 0, "revenue": 0})
            row[f"revenue_{r}"] = round(rev_data["revenue"], 2)
            row[f"subs_{r}"] = rev_data["users"]
        keywords_out.append(row)

    # Ads
    ads_out = []
    for ad in asa_ad_data.values():
        ads_out.append({
            "name": ad["name"],
            "campaign": ad["campaign"],
            "country": ad["country"],
            "cpp_id": ad["cpp_id"],
            "spend": round(ad["spend"], 2),
            "installs": ad["installs"],
            "impressions": ad["impressions"],
            "taps": ad["taps"],
        })

    output = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "campaigns": campaigns_out,
        "keywords": keywords_out,
        "ads": ads_out,
        "totals": {
            r: {
                "spend": sum(row[f"spend_{r}"] for row in campaigns_out),
                "revenue": sum(row[f"revenue_{r}"] for row in campaigns_out),
                "installs": sum(row[f"installs_{r}"] for row in campaigns_out),
                "subs": sum(row[f"subs_{r}"] for row in campaigns_out),
            }
            for r in ranges
        },
    }

    # Save locally
    out_file = "/tmp/data.json"
    with open(out_file, "w") as f:
        json.dump(output, f, indent=None, separators=(",", ":"))
    size_kb = os.path.getsize(out_file) / 1024
    print(f"\nJSON built: {size_kb:.1f} KB")
    print(f"  Campaigns: {len(campaigns_out)}")
    print(f"  Keywords: {len(keywords_out)}")
    print(f"  Ads: {len(ads_out)}")

    # Upload
    print("\n--- Uploading to cPanel ---")
    upload_to_ftp(out_file, "data.json")

    print("\n🎉 Done!")


if __name__ == "__main__":
    main()
