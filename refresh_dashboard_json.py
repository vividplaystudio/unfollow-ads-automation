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


def detect_tier(period_ms: int) -> str:
    """Determine sub tier from billing period duration (ms)."""
    if period_ms <= 0:
        return "other"
    days = period_ms / (1000 * 86400)
    if days < 12:
        return "weekly"
    if days < 200:
        return "monthly"
    return "yearly"


# Counters for one-shot RC API diagnostics in logs
_RC_DEBUG_COUNTER = {"dumped": 0, "empty": 0, "err": 0, "with_items": 0}


def _infer_subscription_transactions(sub: dict) -> list:
    """
    RevenueCat v2 doesn't expose per-transaction line items on /subscriptions.
    Infer them from starts_at + period length + total_revenue.gross.

    Each element is {"ts": ms_epoch, "amount": usd, "is_renewal": bool, "tier": str}.
    Used to bucket revenue by transaction date instead of by cohort (first_seen).
    """
    starts = int(sub.get("starts_at") or 0)
    ends = int(
        sub.get("ends_at")
        or sub.get("current_period_ends_at")
        or 0
    )
    period_start = int(sub.get("current_period_starts_at") or starts)
    period_end = int(sub.get("current_period_ends_at") or ends)
    period_ms = max(0, period_end - period_start)
    total_ms = max(0, ends - starts)

    rev_obj = sub.get("total_revenue_in_usd") or {}
    total_revenue = (
        float(rev_obj.get("gross") or 0)
        if isinstance(rev_obj, dict)
        else float(rev_obj or 0)
    )
    tier = detect_tier(period_ms)

    if total_revenue <= 0 or starts <= 0:
        return []

    if period_ms <= 0 or total_ms <= 0:
        return [{"ts": starts, "amount": total_revenue, "is_renewal": False, "tier": tier}]

    periods = max(1, int(round(total_ms / period_ms)))
    per_period = total_revenue / periods
    return [
        {
            "ts": starts + i * period_ms,
            "amount": per_period,
            "is_renewal": i > 0,
            "tier": tier,
        }
        for i in range(periods)
    ]


def rc_fetch_customer_subs_detail(customer_id: str) -> dict:
    """
    Fetch all subscriptions for a customer and compute:
    - total gross revenue
    - tier breakdown (weekly/monthly/yearly)
    - active + canceled flags
    - estimated renewals count
    - transactions[]: per-charge list {ts, amount, is_renewal, tier} so revenue
      can be bucketed by transaction date (matches RevenueCat dashboard).
    """
    encoded = urllib.parse.quote(customer_id, safe="")
    result = {
        "revenue": 0.0,
        "tier_counts": {"weekly": 0, "monthly": 0, "yearly": 0, "other": 0},
        "tier_revenue": {"weekly": 0.0, "monthly": 0.0, "yearly": 0.0, "other": 0.0},
        "active_tier": None,          # if actively paying
        "is_active": False,           # has any active sub
        "is_canceled": False,         # has any "will_not_renew" sub
        "renewals": 0,                # total renewals across all subs
        "primary_tier": None,         # most recent tier (for labeling)
        "sub_count": 0,
        "transactions": [],           # inferred per-charge history
    }

    # Fetch subscriptions
    url = f"https://api.revenuecat.com/v2/projects/{REVENUECAT_PROJECT_ID}/customers/{encoded}/subscriptions?limit=50"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {REVENUECAT_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            data = json.loads(raw)
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:200]
        except Exception:
            pass
        if _RC_DEBUG_COUNTER["err"] < 3:
            print(f"  RC /subs err {e.code} for {customer_id[:40]}: {err_body}")
            _RC_DEBUG_COUNTER["err"] += 1
        return result
    except Exception as e:
        if _RC_DEBUG_COUNTER["err"] < 3:
            print(f"  RC /subs generic err for {customer_id[:40]}: {e}")
            _RC_DEBUG_COUNTER["err"] += 1
        return result

    # One-shot diagnostic: dump first non-empty response so we can see what
    # fields RC actually returns vs what we're parsing.
    if data.get("items") and _RC_DEBUG_COUNTER["dumped"] < 2:
        _RC_DEBUG_COUNTER["dumped"] += 1
        print(f"  RC /subs sample response (customer {customer_id[:40]}):")
        print("  " + json.dumps(data["items"][0], indent=2)[:1200])
    elif not data.get("items"):
        _RC_DEBUG_COUNTER["empty"] += 1

    if data.get("items"):
        _RC_DEBUG_COUNTER["with_items"] += 1

    latest_starts = 0
    for s in data.get("items", []):
        result["sub_count"] += 1
        gross = 0.0
        rev = s.get("total_revenue_in_usd") or {}
        if isinstance(rev, dict):
            gross = float(rev.get("gross", 0) or 0)
        result["revenue"] += gross

        period_start = s.get("current_period_starts_at") or s.get("starts_at") or 0
        period_end = s.get("current_period_ends_at") or s.get("ends_at") or 0
        period_ms = max(0, (period_end or 0) - (period_start or 0))
        tier = detect_tier(period_ms)
        result["tier_counts"][tier] = result["tier_counts"].get(tier, 0) + 1
        result["tier_revenue"][tier] = result["tier_revenue"].get(tier, 0) + gross

        starts = s.get("starts_at") or period_start
        ends = s.get("ends_at") or period_end
        total_ms = max(0, (ends or 0) - (starts or 0))
        if period_ms > 0 and total_ms > 0:
            periods = total_ms / period_ms
            result["renewals"] += max(0, int(round(periods)) - 1)

        result["transactions"].extend(_infer_subscription_transactions(s))

        status = s.get("status", "")
        auto = s.get("auto_renewal_status", "")

        if status == "active":
            result["is_active"] = True
            if (starts or 0) > latest_starts:
                latest_starts = starts or 0
                result["active_tier"] = tier

        if auto == "will_not_renew":
            result["is_canceled"] = True

        if (starts or 0) > latest_starts and not result["active_tier"]:
            result["primary_tier"] = tier

    # If no active tier set, fall back to most recent sub's tier
    if not result["active_tier"] and not result["primary_tier"] and result["sub_count"] > 0:
        # Use any tier that has count > 0, prefer yearly > monthly > weekly
        for t in ["yearly", "monthly", "weekly"]:
            if result["tier_counts"].get(t, 0) > 0:
                result["primary_tier"] = t
                break

    # Also fetch one-time purchases
    url = f"https://api.revenuecat.com/v2/projects/{REVENUECAT_PROJECT_ID}/customers/{encoded}/purchases?limit=50"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {REVENUECAT_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        for p in data.get("items", []):
            rev = p.get("revenue_in_usd") or p.get("total_revenue_in_usd") or {}
            amount = 0.0
            if isinstance(rev, dict):
                amount = float(rev.get("gross", 0) or 0)
            elif isinstance(rev, (int, float)):
                amount = float(rev)
            result["revenue"] += amount

            purchased_at = int(
                p.get("purchased_at")
                or p.get("store_purchase_identifier_purchase_date")
                or p.get("created_at")
                or 0
            )
            if amount > 0 and purchased_at > 0:
                result["transactions"].append({
                    "ts": purchased_at,
                    "amount": amount,
                    "is_renewal": False,
                    "tier": "other",
                })
    except Exception:
        pass

    return result


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
    For each customer, fetch their attributes + subscriptions in parallel.
    Enrich EVERY customer — older users still paying renewals are a big chunk
    of revenue, and filtering them out was under-counting the dashboard.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    to_enrich = list(customers)
    print(f"  Enriching {len(to_enrich)} customers (no date cutoff)...")

    def enrich_one(customer):
        cid = customer["id"]
        attrs = rc_fetch_customer_attrs(cid)
        customer["_attrs"] = attrs
        # Fetch subscription detail for ALL customers so we can track every channel's revenue
        subs = rc_fetch_customer_subs_detail(cid)
        customer["_revenue"] = subs["revenue"]
        customer["_active"] = subs["is_active"]
        customer["_canceled"] = subs["is_canceled"]
        customer["_renewals"] = subs["renewals"]
        customer["_tier"] = subs["active_tier"] or subs["primary_tier"] or "none"
        customer["_tier_counts"] = subs["tier_counts"]
        customer["_tier_revenue"] = subs["tier_revenue"]
        customer["_sub_count"] = subs["sub_count"]
        customer["_transactions"] = subs.get("transactions", [])
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
    with_subs = sum(1 for c in enriched if (c.get("_sub_count") or 0) > 0)
    with_txns = sum(1 for c in enriched if c.get("_transactions"))
    total_rev = sum(float(c.get("_revenue") or 0) for c in enriched)
    active_count = sum(1 for c in enriched if c.get("_active"))

    # Media-source distribution — helps spot mis-categorized channels
    from collections import Counter
    src_counter = Counter(
        (c.get("_attrs", {}).get("$mediaSource") or "").strip() or "(empty)"
        for c in enriched
    )
    print(f"  Enriched: {len(enriched)} total | ASA: {asa_count}")
    print(f"  Customers with subs: {with_subs} | with transactions: {with_txns} | currently active: {active_count}")
    print(f"  Sum of _revenue (all customers): ${total_rev:.2f}")
    print(f"  RC /subs diagnostic: dumped_samples={_RC_DEBUG_COUNTER['dumped']}, "
          f"errors={_RC_DEBUG_COUNTER['err']}, empty_responses={_RC_DEBUG_COUNTER['empty']}, "
          f"with_items={_RC_DEBUG_COUNTER['with_items']}")
    print(f"  Top media sources: {src_counter.most_common(10)}")
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
    """
    Build per-campaign / keyword / channel metrics.

    Revenue, renewals, and per-tier revenue are bucketed by TRANSACTION date
    (matches the RevenueCat dashboard). Cohort-style counts (users, active,
    canceled, paid_subs) stay tied to first_seen_at because they describe
    who was *acquired* in the window.
    """
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

    def _zero():
        return {
            "users": 0, "paid_subs": 0, "revenue": 0.0, "active": 0,
            "canceled": 0, "renewals": 0,
            "weekly_subs": 0, "monthly_subs": 0, "yearly_subs": 0,
            "weekly_rev": 0.0, "monthly_rev": 0.0, "yearly_rev": 0.0,
        }

    def _dt_in_range(dt, r):
        if r == "yesterday":
            return yesterday_start <= dt < today_start
        start = ranges[r]
        if start is None:
            return True
        return dt >= start

    by_kw = defaultdict(lambda: defaultdict(_zero))
    by_camp = defaultdict(lambda: defaultdict(_zero))
    by_adgroup = defaultdict(lambda: defaultdict(_zero))
    by_country = defaultdict(lambda: defaultdict(_zero))
    by_channel = defaultdict(lambda: defaultdict(_zero))  # channel = media source

    for c in customers:
        first_seen_ms = c.get("first_seen_at")
        if not first_seen_ms:
            continue
        first_seen = datetime.fromtimestamp(first_seen_ms / 1000, tz=timezone.utc)

        attrs = c.get("_attrs", {})
        media_source = attrs.get("$mediaSource", "").strip()

        # Normalize channel name
        if media_source == "Apple Search Ads":
            channel = "Apple Search Ads"
        elif not media_source:
            channel = "Organic / Unattributed"
        elif "facebook" in media_source.lower() or "meta" in media_source.lower():
            channel = "Facebook / Meta"
        elif "google" in media_source.lower():
            channel = "Google Ads"
        elif "tiktok" in media_source.lower():
            channel = "TikTok"
        else:
            channel = media_source  # keep as-is

        campaign = attrs.get("$campaign", "")
        keyword = attrs.get("$keyword", "").lower()
        adgroup = attrs.get("$adGroup", "")
        country = c.get("last_seen_country", "")

        is_active = 1 if c.get("_active") else 0
        is_canceled = 1 if c.get("_canceled") else 0
        transactions = c.get("_transactions") or []

        # Pre-aggregate transactions per window (transaction-date buckets).
        txn_by_range = {r: {"revenue": 0.0, "renewals": 0,
                            "weekly_rev": 0.0, "monthly_rev": 0.0, "yearly_rev": 0.0,
                            "paid_in_range": False}
                        for r in ranges}
        for t in transactions:
            ts = t.get("ts") or 0
            if ts <= 0:
                continue
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            amount = float(t.get("amount") or 0)
            tier = t.get("tier") or "other"
            is_renewal = bool(t.get("is_renewal"))
            for r in ranges:
                if _dt_in_range(dt, r):
                    bucket = txn_by_range[r]
                    bucket["revenue"] += amount
                    if is_renewal:
                        bucket["renewals"] += 1
                    if tier in ("weekly", "monthly", "yearly"):
                        bucket[f"{tier}_rev"] += amount
                    bucket["paid_in_range"] = True

        def _apply(b, r, include_cohort):
            """include_cohort=True → also add users/active/canceled/new-sub counts."""
            txn = txn_by_range[r]
            b["revenue"] += txn["revenue"]
            b["renewals"] += txn["renewals"]
            b["weekly_rev"] += txn["weekly_rev"]
            b["monthly_rev"] += txn["monthly_rev"]
            b["yearly_rev"] += txn["yearly_rev"]
            # paid_subs = customers with any transaction in the window
            if txn["paid_in_range"]:
                b["paid_subs"] += 1
            if include_cohort:
                b["users"] += 1
                b["active"] += is_active
                b["canceled"] += is_canceled
                tier_counts = c.get("_tier_counts") or {}
                b["weekly_subs"] += tier_counts.get("weekly", 0)
                b["monthly_subs"] += tier_counts.get("monthly", 0)
                b["yearly_subs"] += tier_counts.get("yearly", 0)

        for r in ranges:
            # Cohort check: customer was acquired in this window
            in_cohort = _dt_in_range(first_seen, r)
            # Include the row if either cohort-acquired OR had a transaction in the window
            if not in_cohort and not txn_by_range[r]["paid_in_range"]:
                continue

            _apply(by_channel[channel][r], r, in_cohort)

            if channel == "Apple Search Ads":
                _apply(by_kw[(campaign, keyword)][r], r, in_cohort)
                _apply(by_camp[campaign][r], r, in_cohort)
                _apply(by_adgroup[(campaign, adgroup)][r], r, in_cohort)
                _apply(by_country[country][r], r, in_cohort)

    return {
        "by_keyword": by_kw,
        "by_campaign": by_camp,
        "by_adgroup": by_adgroup,
        "by_country": by_country,
        "by_channel": by_channel,
    }


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

    asa_campaign_data = {r: {} for r in ranges}
    asa_keyword_data = {r: {} for r in ranges}
    asa_ad_data = {}  # keyed by (campaign_id, ad_name), multi-range within each

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
                    "ad_group_id": m.get("adGroupId"),
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

            # Fetch ads for all ranges
            ad_rows = asa_report(asa_token, "ads", start_s, end_s, campaign_id=cid)
            for row in ad_rows:
                m = row.get("metadata", {})
                t = row.get("total", {})
                ad_name = m.get("adName") or m.get("name") or "—"
                key = (cid, ad_name)
                if key not in asa_ad_data:
                    asa_ad_data[key] = {
                        "name": ad_name,
                        "campaign_id": cid,
                        "campaign": meta["name"],
                        "country": get_country_from_campaign(meta["name"], meta["countries"]),
                        "cpp_id": m.get("cppId", ""),
                    }
                asa_ad_data[key][f"spend_{range_name}"] = float(t.get("localSpend", {}).get("amount", 0) or 0)
                asa_ad_data[key][f"impressions_{range_name}"] = int(t.get("impressions", 0) or 0)
                asa_ad_data[key][f"taps_{range_name}"] = int(t.get("taps", 0) or 0)
                asa_ad_data[key][f"installs_{range_name}"] = int(t.get("totalInstalls", 0) or 0)

        print(f"  Keywords this range: {len(asa_keyword_data[range_name])}")

    # Fetch RevenueCat
    print("\n--- Fetching RevenueCat ---")
    customers = rc_get_all_customers()
    customers = rc_enrich_customers(customers)
    rev_index = build_revenue_index(customers)

    # Build unified output
    print("\nBuilding JSON...")
    print(f"  Ads collected: {len(asa_ad_data)}")

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

            rev_data = rev_index["by_campaign"].get(meta["name"], {}).get(r) or {}
            row[f"revenue_{r}"] = round(rev_data.get("revenue", 0), 2)
            row[f"subs_{r}"] = rev_data.get("paid_subs", 0)
            row[f"asa_users_{r}"] = rev_data.get("users", 0)
            row[f"active_{r}"] = rev_data.get("active", 0)
            row[f"canceled_{r}"] = rev_data.get("canceled", 0)
            row[f"renewals_{r}"] = rev_data.get("renewals", 0)
            row[f"weekly_subs_{r}"] = rev_data.get("weekly_subs", 0)
            row[f"monthly_subs_{r}"] = rev_data.get("monthly_subs", 0)
            row[f"yearly_subs_{r}"] = rev_data.get("yearly_subs", 0)
            row[f"weekly_rev_{r}"] = round(rev_data.get("weekly_rev", 0), 2)
            row[f"monthly_rev_{r}"] = round(rev_data.get("monthly_rev", 0), 2)
            row[f"yearly_rev_{r}"] = round(rev_data.get("yearly_rev", 0), 2)
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
        ad_group_id = None
        keyword_id = None
        for r in ["all", "30d", "14d", "7d", "today", "yesterday"]:
            d = asa_keyword_data[r].get((cid, kw_lower))
            if d:
                kw_display = d["keyword"]
                match = d.get("match") or match
                status = d.get("status") or status
                bid = d.get("bid") or bid
                is_share = d.get("is") or is_share
                ad_group_id = d.get("ad_group_id") or ad_group_id
                keyword_id = d.get("keyword_id") or keyword_id
                break

        row = {
            "keyword": kw_display,
            "campaign_id": cid,
            "ad_group_id": ad_group_id,
            "keyword_id": keyword_id,
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

            rev_data = rev_index["by_keyword"].get((meta.get("name", ""), kw_lower), {}).get(r) or {}
            row[f"revenue_{r}"] = round(rev_data.get("revenue", 0), 2)
            row[f"subs_{r}"] = rev_data.get("paid_subs", 0)
            row[f"asa_users_{r}"] = rev_data.get("users", 0)
            row[f"active_{r}"] = rev_data.get("active", 0)
            row[f"canceled_{r}"] = rev_data.get("canceled", 0)
            row[f"renewals_{r}"] = rev_data.get("renewals", 0)
            row[f"weekly_subs_{r}"] = rev_data.get("weekly_subs", 0)
            row[f"monthly_subs_{r}"] = rev_data.get("monthly_subs", 0)
            row[f"yearly_subs_{r}"] = rev_data.get("yearly_subs", 0)
        keywords_out.append(row)

    # Ads — with per-range metrics
    ads_out = []
    for ad in asa_ad_data.values():
        row = {
            "name": ad["name"],
            "campaign": ad["campaign"],
            "campaign_id": ad["campaign_id"],
            "country": ad["country"],
            "cpp_id": ad["cpp_id"],
        }
        for r in ranges:
            row[f"spend_{r}"] = round(ad.get(f"spend_{r}", 0), 2)
            row[f"installs_{r}"] = ad.get(f"installs_{r}", 0)
            row[f"impressions_{r}"] = ad.get(f"impressions_{r}", 0)
            row[f"taps_{r}"] = ad.get(f"taps_{r}", 0)
        ads_out.append(row)

    # Ad groups
    adgroups_out = []
    for (camp_name, adgroup_name), _ in list(rev_index["by_adgroup"].items()):
        if not adgroup_name:
            continue
        row = {
            "ad_group": adgroup_name,
            "campaign": camp_name,
        }
        for r in ranges:
            rev_data = rev_index["by_adgroup"].get((camp_name, adgroup_name), {}).get(r) or {}
            row[f"revenue_{r}"] = round(rev_data.get("revenue", 0), 2)
            row[f"subs_{r}"] = rev_data.get("paid_subs", 0)
            row[f"active_{r}"] = rev_data.get("active", 0)
            row[f"canceled_{r}"] = rev_data.get("canceled", 0)
            row[f"renewals_{r}"] = rev_data.get("renewals", 0)
            row[f"weekly_subs_{r}"] = rev_data.get("weekly_subs", 0)
            row[f"monthly_subs_{r}"] = rev_data.get("monthly_subs", 0)
            row[f"yearly_subs_{r}"] = rev_data.get("yearly_subs", 0)
        adgroups_out.append(row)

    # Channels breakdown (includes non-ASA sources — organic, Meta, etc.)
    channels_out = []
    for channel_name, range_data in rev_index["by_channel"].items():
        row = {"channel": channel_name}
        for r in ranges:
            rd = range_data.get(r) or {}
            row[f"users_{r}"] = rd.get("users", 0)
            row[f"subs_{r}"] = rd.get("paid_subs", 0)
            row[f"revenue_{r}"] = round(rd.get("revenue", 0), 2)
            row[f"active_{r}"] = rd.get("active", 0)
            row[f"canceled_{r}"] = rd.get("canceled", 0)
            row[f"renewals_{r}"] = rd.get("renewals", 0)
            row[f"weekly_subs_{r}"] = rd.get("weekly_subs", 0)
            row[f"monthly_subs_{r}"] = rd.get("monthly_subs", 0)
            row[f"yearly_subs_{r}"] = rd.get("yearly_subs", 0)
        channels_out.append(row)

    output = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "campaigns": campaigns_out,
        "keywords": keywords_out,
        "ads": ads_out,
        "ad_groups": adgroups_out,
        "channels": channels_out,
        "totals": {
            r: {
                "spend": round(sum(row[f"spend_{r}"] for row in campaigns_out), 2),
                "revenue": round(sum(row[f"revenue_{r}"] for row in campaigns_out), 2),
                "installs": sum(row[f"installs_{r}"] for row in campaigns_out),
                "subs": sum(row[f"subs_{r}"] for row in campaigns_out),
                "asa_users": sum(row[f"asa_users_{r}"] for row in campaigns_out),
                "active": sum(row[f"active_{r}"] for row in campaigns_out),
                "canceled": sum(row[f"canceled_{r}"] for row in campaigns_out),
                "renewals": sum(row[f"renewals_{r}"] for row in campaigns_out),
                "weekly_subs": sum(row[f"weekly_subs_{r}"] for row in campaigns_out),
                "monthly_subs": sum(row[f"monthly_subs_{r}"] for row in campaigns_out),
                "yearly_subs": sum(row[f"yearly_subs_{r}"] for row in campaigns_out),
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
