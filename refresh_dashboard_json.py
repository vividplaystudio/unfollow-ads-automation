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
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone


# ASA env vars are now optional — the script can run without them and just
# skip the ASA fetch (since the cPanel cron is RC-focused; the old GitHub
# Actions setup needed the ASA token via Google Sheets).
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
ASA_ENABLED = bool(SPREADSHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON)
# REVENUECAT_API_KEY is required by the full refresh (rc_get_all_customers
# and rc_enrich_customers), but the fast daily_rc path imports from this
# module without needing the API key (it reads webhook events instead).
# Use .get() so the import succeeds; the full-refresh main() validates
# below.
REVENUECAT_API_KEY = os.environ.get("REVENUECAT_API_KEY", "")
REVENUECAT_PROJECT_ID = os.environ.get("REVENUECAT_PROJECT_ID", "6afc72a9")
RC_WEBHOOK_SECRET = os.environ.get("RC_WEBHOOK_SECRET", "").strip()
RC_EVENTS_URL = os.environ.get(
    "RC_EVENTS_URL",
    "https://genivox.com/ads-upload/rc_events.php",
)
ORG_ID = os.environ.get("ASA_ORG_ID", "8868820")

# When running ON the cPanel host, set LOCAL_OUTPUT_DIR to the absolute
# dashboard folder and the script will write directly there — no FTP.
LOCAL_OUTPUT_DIR = os.environ.get("LOCAL_OUTPUT_DIR", "")

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
        # Retry up to 3 times on transient errors (timeout, 5xx, connection
        # reset). RC's v2 customers endpoint occasionally hangs >60s and
        # without retries the whole refresh aborts, leaving the dashboard
        # frozen for hours until the next cron tick recovers — or doesn't.
        data = None
        last_err = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode())
                break
            except urllib.error.HTTPError as e:
                if e.code in (500, 502, 503, 504) and attempt < 2:
                    last_err = f"HTTP {e.code}"
                    print(f"  RC API {last_err} on page {page}, retry {attempt+1}/3")
                    time.sleep(2 ** attempt)
                    continue
                print(f"  RC API error: {e.code} on page {page}")
                last_err = f"HTTP {e.code}"
                break
            except (TimeoutError, urllib.error.URLError, ConnectionResetError) as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt < 2:
                    print(f"  RC API {last_err} on page {page}, retry {attempt+1}/3")
                    time.sleep(2 ** attempt)
                    continue
                print(f"  RC API timeout on page {page} after 3 retries: {last_err}")
                break
        if data is None:
            # Hard failure after retries — surface a clear error so the
            # caller can decide what to do instead of silently producing a
            # partial customer list that under-counts daily_rc.
            raise RuntimeError(
                f"rc_get_all_customers: gave up at page {page} after retries ({last_err}). "
                f"Got {len(all_customers)} customers so far."
            )
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
    try:
        data = _rc_get_json(url)
    except Exception:
        _RC_DEBUG_COUNTER["attrs_err"] = _RC_DEBUG_COUNTER.get("attrs_err", 0) + 1
        return {}
    result = {}
    for item in data.get("items", []):
        result[item.get("name", "")] = item.get("value", "")
    return result


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


def _classify_product_tier(product_id: str, period_type: str = "") -> str:
    """Best-effort tier inference from RevenueCat product_id / period_type."""
    pl = (product_id or "").lower()
    if "year" in pl or "annual" in pl:
        return "yearly"
    if "month" in pl:
        return "monthly"
    if "week" in pl:
        return "weekly"
    return "other"


_LAST_REFUND_SUMMARY = {}  # populated as side effect of fetch_webhook_events()


def fetch_webhook_events() -> dict:
    """
    Pull captured RC webhook events from the server endpoint and return a
    map {app_user_id: [{ts, amount, is_renewal, tier}]} suitable for the same
    bucketing code the inference path uses.

    Returns empty dict if RC_WEBHOOK_SECRET is not configured — in that case
    we fall back to inference for every customer, which is what the script
    was doing before webhooks existed.

    Side effect: populates _LAST_REFUND_SUMMARY with refund stats from the
    same fetch, so main() can include it in the output without re-pulling.
    """
    global _LAST_REFUND_SUMMARY

    # Only fetch the last ~60 days of events. Without since_ms the PHP endpoint
    # streams the log from the OLDEST line forward and cuts at limit=50000 —
    # once the log exceeds 50k events the most recent days silently fall off
    # the end, causing daily_rc to undercount newer days.
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=60)).timestamp() * 1000)

    # PREFERRED PATH: read rc_events.jsonl from disk directly when this script
    # is running on the cPanel host (LOCAL_OUTPUT_DIR is set and the log file
    # is in that directory). This skips Apache entirely, which is important
    # because the /ads-upload folder is behind HTTP Basic Auth and HTTP only
    # allows a single Authorization header per request — we can't carry both
    # basic auth (folder) and bearer (PHP-level) at the same time. Reading
    # from disk has neither problem.
    events = None
    skipped_count = 0
    if LOCAL_OUTPUT_DIR:
        local_log = os.path.join(LOCAL_OUTPUT_DIR, "rc_events.jsonl")
        if os.path.exists(local_log):
            try:
                events = []
                with open(local_log, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        ev = rec.get("event") or {}
                        ts = int(ev.get("purchased_at_ms")
                                 or ev.get("event_timestamp_ms")
                                 or 0)
                        if ts > 0 and ts < since_ms:
                            skipped_count += 1
                            continue
                        events.append(rec)
                print(f"  [webhooks] read {len(events)} events from local "
                      f"{local_log} (skipped {skipped_count} older than 60d)")
            except Exception as e:
                print(f"  [webhooks] local read failed ({type(e).__name__}: {e}); "
                      "falling back to HTTP")
                events = None

    # HTTP fallback for when not running on cPanel.
    if events is None:
        if not RC_WEBHOOK_SECRET:
            print("  [webhooks] RC_WEBHOOK_SECRET not set and no local log — "
                  "skipping webhook fetch")
            _LAST_REFUND_SUMMARY = {}
            return {}

        # Pass the bearer via ?token=... so the basic-auth Authorization
        # header (when set by an HTTP client middleware) doesn't collide.
        url = f"{RC_EVENTS_URL}?since_ms={since_ms}&limit=50000" \
              f"&token={urllib.parse.quote(RC_WEBHOOK_SECRET, safe='')}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {RC_WEBHOOK_SECRET}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            print(f"  [webhooks] fetch failed: {type(e).__name__}: {e}")
            _LAST_REFUND_SUMMARY = {}
            return {}

        events = data.get("events", [])
        skipped_count = data.get("skipped_before_since", 0)
        print(f"  [webhooks] fetched {len(events)} events over HTTP "
              f"(skipped {skipped_count} older than 60d)")

    revenue_types = {"INITIAL_PURCHASE", "RENEWAL", "NON_RENEWING_PURCHASE", "PRODUCT_CHANGE"}
    by_user = defaultdict(list)
    total_revenue = 0.0
    counted_by_type = defaultdict(int)

    # Refund tracking — RC fires CANCELLATION events with cancel_reason ∈
    # {REFUND, CUSTOMER_SUPPORT} for refunds, plus standalone REFUND events
    # in newer schema. Either way we track them as a refund.
    today = datetime.now(timezone.utc)
    refunds_total_count = 0
    refunds_total_amount = 0.0
    refunds_30d_count = 0
    refunds_30d_amount = 0.0
    refunds_by_day = defaultdict(lambda: {"count": 0, "amount": 0.0})

    for rec in events:
        ev = rec.get("event") or {}
        etype = ev.get("type")
        ts = int(ev.get("purchased_at_ms") or ev.get("event_timestamp_ms") or 0)
        price = float(ev.get("price") or 0)

        # Detect refund events
        is_refund = etype == "REFUND" or (
            etype == "CANCELLATION"
            and (ev.get("cancel_reason") or "").upper() in {"REFUND", "CUSTOMER_SUPPORT"}
        )
        if is_refund and ts > 0:
            amount = abs(price)
            refunds_total_count += 1
            refunds_total_amount += amount
            day_key = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()
            refunds_by_day[day_key]["count"] += 1
            refunds_by_day[day_key]["amount"] += amount
            ev_dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            if (today - ev_dt).days <= 30:
                refunds_30d_count += 1
                refunds_30d_amount += amount

        # Standard revenue events
        if etype not in revenue_types:
            continue
        app_user_id = ev.get("app_user_id") or ev.get("original_app_user_id")
        if not app_user_id or price <= 0 or ts <= 0:
            continue
        tier = _classify_product_tier(
            ev.get("product_id", ""), ev.get("period_type", "")
        )
        by_user[app_user_id].append({
            "ts": ts,
            "amount": price,
            "is_renewal": etype == "RENEWAL",
            "tier": tier,
        })
        total_revenue += price
        counted_by_type[etype] += 1

    # Build last-30d daily refunds list
    refunds_30d_daily = []
    for offset in range(30):
        d = (today.date() - timedelta(days=offset)).isoformat()
        r = refunds_by_day.get(d, {"count": 0, "amount": 0.0})
        refunds_30d_daily.append({
            "date": d,
            "count": r["count"],
            "amount": round(r["amount"], 2),
        })
    refunds_30d_daily.reverse()  # oldest first

    _LAST_REFUND_SUMMARY = {
        "total_count": refunds_total_count,
        "total_amount": round(refunds_total_amount, 2),
        "last_30d_count": refunds_30d_count,
        "last_30d_amount": round(refunds_30d_amount, 2),
        "daily_30d": refunds_30d_daily,
    }

    print(
        f"  [webhooks] indexed {sum(len(v) for v in by_user.values())} "
        f"revenue events for {len(by_user)} users — total ${total_revenue:.2f} — "
        f"by type: {dict(counted_by_type)}"
    )
    print(
        f"  [webhooks] refunds: all-time {refunds_total_count} (${refunds_total_amount:.2f}) "
        f"· 30d {refunds_30d_count} (${refunds_30d_amount:.2f})"
    )
    return dict(by_user)


def _rc_get_json(url: str, timeout: int = 30, max_retries: int = 6) -> dict:
    """
    GET against RevenueCat v2 with exponential-backoff retries on 429 and 5xx.
    Returns parsed JSON on success or raises the last exception.
    """
    import time as _time
    delay = 1.0
    last_exc = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {REVENUECAT_API_KEY}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code == 429 or 500 <= e.code < 600:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(retry_after) if retry_after else delay
                except ValueError:
                    wait = delay
                _time.sleep(min(wait, 30))
                delay = min(delay * 2, 30)
                continue
            raise
        except Exception as e:
            last_exc = e
            _time.sleep(delay)
            delay = min(delay * 2, 30)
    raise last_exc if last_exc else RuntimeError("rc_get_json failed without exception")


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

    # Fetch subscriptions (with retry/backoff on 429)
    url = f"https://api.revenuecat.com/v2/projects/{REVENUECAT_PROJECT_ID}/customers/{encoded}/subscriptions?limit=50"
    try:
        data = _rc_get_json(url)
    except urllib.error.HTTPError as e:
        _RC_DEBUG_COUNTER["err"] += 1
        _RC_DEBUG_COUNTER[f"err_{e.code}"] = _RC_DEBUG_COUNTER.get(f"err_{e.code}", 0) + 1
        if _RC_DEBUG_COUNTER["err"] <= 3:
            print(f"  RC /subs err {e.code} for {customer_id[:40]}")
        return result
    except Exception as e:
        _RC_DEBUG_COUNTER["err"] += 1
        _RC_DEBUG_COUNTER["err_other"] = _RC_DEBUG_COUNTER.get("err_other", 0) + 1
        if _RC_DEBUG_COUNTER["err"] <= 3:
            print(f"  RC /subs generic err for {customer_id[:40]}: {type(e).__name__}: {e}")
        return result

    # Probe /invoices on customers that look like they have renewal history
    # (current_period_starts_at > starts_at). Stop after we find one with
    # non-empty invoices so we can see the actual shape.
    if data.get("items") and _RC_DEBUG_COUNTER["dumped"] < 5:
        sub = data["items"][0]
        starts = int(sub.get("starts_at") or 0)
        cur_start = int(sub.get("current_period_starts_at") or 0)
        if cur_start > starts:  # has renewals
            _RC_DEBUG_COUNTER["dumped"] += 1
            sub_id = sub.get("id")
            inv_url = (
                f"https://api.revenuecat.com/v2/projects/"
                f"{REVENUECAT_PROJECT_ID}/customers/{encoded}/invoices?limit=20"
            )
            try:
                inv = _rc_get_json(inv_url, max_retries=1)
                items = inv.get("items", [])
                print(f"  [PROBE] /invoices -> {len(items)} items "
                      f"(customer has {(cur_start-starts)//(7*86400000)}w of renewals)")
                if items:
                    print(f"  [PROBE]   item[0] keys: {sorted(items[0].keys())}")
                    for k in ("paid_at", "invoice_date", "created_at",
                              "period_start", "period_end", "amount",
                              "amount_in_usd", "total_in_usd", "revenue_in_usd",
                              "status", "type", "kind"):
                        v = items[0].get(k)
                        if v is not None:
                            print(f"  [PROBE]     {k} = {v!r}")
            except urllib.error.HTTPError as e:
                print(f"  [PROBE] /invoices -> HTTP {e.code}")
            except Exception as e:
                print(f"  [PROBE] /invoices -> {type(e).__name__}: {e}")
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

    # Also fetch one-time purchases (with retry/backoff)
    url = f"https://api.revenuecat.com/v2/projects/{REVENUECAT_PROJECT_ID}/customers/{encoded}/purchases?limit=50"
    try:
        data = _rc_get_json(url)
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

    Revenue comes from webhook events when available (100% accurate, with real
    transaction dates) and falls back to inference for customers with no
    webhook events yet (e.g. historical customers from before webhooks were
    configured, or transactions outside the webhook log window).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    webhook_events = fetch_webhook_events()
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

        # Prefer webhook-sourced transactions (exact amounts + exact dates)
        # over inferred ones. Inference runs first so the fallback is always
        # populated; webhook events override if we have them for this user.
        webhook_txns = webhook_events.get(cid)
        if webhook_txns:
            customer["_transactions"] = webhook_txns
            customer["_txn_source"] = "webhook"
        else:
            customer["_transactions"] = subs.get("transactions", [])
            customer["_txn_source"] = "inference"
        return customer

    enriched = []
    done = 0
    # Lower concurrency to stay under RevenueCat's v2 rate limits.
    # Previous value (25) triggered ~4k HTTP 429s in a single run.
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(enrich_one, c) for c in to_enrich]
        for future in as_completed(futures):
            enriched.append(future.result())
            done += 1
            if done % 100 == 0:
                print(f"    {done}/{len(to_enrich)}")

    asa_count = sum(1 for c in enriched if c.get("_attrs", {}).get("$mediaSource") == "Apple Search Ads")
    with_subs = sum(1 for c in enriched if (c.get("_sub_count") or 0) > 0)
    with_txns = sum(1 for c in enriched if c.get("_transactions"))
    from_webhook = sum(1 for c in enriched if c.get("_txn_source") == "webhook")
    from_inference = sum(1 for c in enriched if c.get("_txn_source") == "inference" and c.get("_transactions"))
    total_rev = sum(float(c.get("_revenue") or 0) for c in enriched)
    active_count = sum(1 for c in enriched if c.get("_active"))
    print(f"  Transaction sources: webhook={from_webhook} inference={from_inference}")

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
    err_breakdown = {k: v for k, v in _RC_DEBUG_COUNTER.items() if k.startswith("err_")}
    if err_breakdown:
        print(f"  RC error breakdown: {err_breakdown}")
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


def compute_daily_rc(customers, days: int = 30) -> list:
    """Per-day RC aggregation for the last `days` days (UTC).

    Buckets each transaction by its purchase date and counts:
      - revenue, weekly/monthly/yearly revenue
      - new_subs (customer's first paid transaction on that day)
      - renewals (any transaction marked is_renewal)
      - per-tier new-sub counts
      - canceled (best-effort: customers whose subscription expired on day
        without renewal — uses _canceled_at if enrich set it, else 0)

    Returns a list of {date, revenue, new_subs, renewals, weekly_*,
    monthly_*, yearly_*} sorted oldest → newest.
    """
    today = datetime.now(timezone.utc).date()
    daily = {}
    for offset in range(days):
        d = (today - timedelta(days=offset)).isoformat()
        daily[d] = {
            "date": d,
            "revenue": 0.0,
            "new_subs": 0,
            "renewals": 0,
            "canceled": 0,
            "weekly_count": 0, "monthly_count": 0, "yearly_count": 0,
            "weekly_rev": 0.0, "monthly_rev": 0.0, "yearly_rev": 0.0,
        }

    for c in customers:
        transactions = c.get("_transactions") or []
        sorted_txs = sorted(transactions, key=lambda t: t.get("ts") or 0)
        first_paid_seen = False

        for t in sorted_txs:
            ts = t.get("ts") or 0
            if ts <= 0:
                continue
            day_key = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()
            if day_key not in daily:
                # Track whether we've seen the first paid tx (it might be older
                # than the 30d window) so day-of-first-purchase is correct
                if not bool(t.get("is_renewal")):
                    first_paid_seen = True
                continue

            amount = float(t.get("amount") or 0)
            tier = t.get("tier") or "other"
            is_renewal = bool(t.get("is_renewal"))
            d = daily[day_key]

            d["revenue"] += amount
            if is_renewal:
                d["renewals"] += 1
            elif not first_paid_seen:
                # First-ever paid transaction for this customer
                d["new_subs"] += 1
                first_paid_seen = True
                if tier == "weekly":
                    d["weekly_count"] += 1
                elif tier == "monthly":
                    d["monthly_count"] += 1
                elif tier == "yearly":
                    d["yearly_count"] += 1

            if tier == "weekly":
                d["weekly_rev"] += amount
            elif tier == "monthly":
                d["monthly_rev"] += amount
            elif tier == "yearly":
                d["yearly_rev"] += amount

    out = []
    for d in sorted(daily.values(), key=lambda x: x["date"]):
        d["revenue"] = round(d["revenue"], 2)
        d["weekly_rev"] = round(d["weekly_rev"], 2)
        d["monthly_rev"] = round(d["monthly_rev"], 2)
        d["yearly_rev"] = round(d["yearly_rev"], 2)
        out.append(d)
    return out


def compute_cohort_retention(customers) -> dict:
    """Per-tier subscription retention curves.

    For each tier (weekly/monthly/yearly), count what % of customers in a
    cohort N days old still have an active subscription at day N. We use
    transaction count as the survival proxy:

      D{n} retained ⟺ customer has ≥ ((n / cycle_days) + 1) transactions
      of this tier (initial purchase + n/cycle renewals)

    Cohort is filtered to customers whose FIRST paid tx was ≥ N days ago,
    so D28 measures only people who've had a chance to renew 4 times.

    Returns:
      {
        "weekly":  {"D7": {cohort_size, retained, rate, required_txs}, ...},
        "monthly": {...},
        "yearly":  {...},
      }
    """
    today = datetime.now(timezone.utc)

    tier_specs = {
        "weekly":  {"cycle": 7,   "checkpoints": [7, 14, 28, 56, 84]},
        "monthly": {"cycle": 30,  "checkpoints": [30, 60, 90, 180]},
        "yearly":  {"cycle": 365, "checkpoints": [365]},
    }

    # Pre-extract per-customer (first_tier, first_dt, tier_tx_counts)
    per_cust = []
    for c in customers:
        txs = sorted(c.get("_transactions") or [], key=lambda t: t.get("ts") or 0)
        if not txs:
            continue
        first_tier = txs[0].get("tier")
        first_ts = txs[0].get("ts")
        if not first_ts or first_tier not in tier_specs:
            continue
        first_dt = datetime.fromtimestamp(first_ts / 1000, tz=timezone.utc)
        tier_tx_counts = {"weekly": 0, "monthly": 0, "yearly": 0}
        for t in txs:
            tt = t.get("tier")
            if tt in tier_tx_counts:
                tier_tx_counts[tt] += 1
        per_cust.append({
            "first_tier": first_tier,
            "first_dt": first_dt,
            "tier_tx_counts": tier_tx_counts,
            "is_active": bool(c.get("_active")),
        })

    results = {}
    for tier, spec in tier_specs.items():
        cycle = spec["cycle"]
        results[tier] = {}
        for d in spec["checkpoints"]:
            required_txs = (d // cycle) + 1
            cohort_size = 0
            retained = 0
            for cust in per_cust:
                if cust["first_tier"] != tier:
                    continue
                days_since = (today - cust["first_dt"]).days
                if days_since < d:
                    continue
                cohort_size += 1
                if cust["tier_tx_counts"][tier] >= required_txs:
                    retained += 1
            results[tier][f"D{d}"] = {
                "cohort_size": cohort_size,
                "retained": retained,
                "rate": round(retained / cohort_size * 100, 1) if cohort_size else 0,
                "required_txs": required_txs,
            }
    return results


# ══════════════════════════════════════════════════════════════════
# FTP upload
# ══════════════════════════════════════════════════════════════════

def publish_output(local_file: str, remote_name: str) -> None:
    """Publish JSON to the dashboard. When LOCAL_OUTPUT_DIR is set (script
    is running on cPanel), copy directly — no FTP. Else FTPS upload."""
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
    # Validate REVENUECAT_API_KEY here (not at module load) so the fast
    # daily_rc path — which imports from this module but doesn't hit RC's
    # v2 API — can run on a host that hasn't set this env var.
    if not REVENUECAT_API_KEY:
        print(
            "ERROR: REVENUECAT_API_KEY env var is not set. "
            "Full refresh cannot run; use refresh_daily_rc_fast.py for the "
            "webhook-only path.",
            file=sys.stderr,
        )
        sys.exit(1)

    if ASA_ENABLED:
        google_token = get_google_access_token()
        config = sheets_read(google_token, "_Config!B1:B1")
        if not config or not config[0]:
            print("⚠ ASA token cell empty — running RC-only.")
            asa_token = None
        else:
            asa_token = config[0][0]
    else:
        print("⚠ SPREADSHEET_ID / GOOGLE_SERVICE_ACCOUNT_JSON not set — skipping ASA fetch, running RC-only.")
        asa_token = None

    if asa_token:
        print("Fetching campaigns...")
        campaigns = asa_get_campaigns(asa_token)
        print(f"Found {len(campaigns)} campaigns")
    else:
        campaigns = []

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
        if not asa_token:
            break  # nothing to fetch from ASA, skip the whole loop
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
    daily_rc = compute_daily_rc(customers, days=30)
    print(f"  Daily RC: {len(daily_rc)} days, "
          f"latest revenue ${daily_rc[-1]['revenue']:.2f}")
    cohort_retention = compute_cohort_retention(customers)
    if cohort_retention.get("weekly", {}).get("D7"):
        d7 = cohort_retention["weekly"]["D7"]
        print(f"  Weekly D7 retention: {d7['rate']}% ({d7['retained']}/{d7['cohort_size']})")

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
        "daily_rc": daily_rc,
        "cohort_retention": cohort_retention,
        "refunds": _LAST_REFUND_SUMMARY,
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
    publish_output(out_file, "data.json")

    print("\n🎉 Done!")


if __name__ == "__main__":
    main()
