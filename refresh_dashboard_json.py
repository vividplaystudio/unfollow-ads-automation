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
# ── Apple Ads Platform API v1 ──────────────────────────────────────────────
# v1 mints its own token from the same credentials refresh_token.py already
# uses (identical ES256 JWT, same audience, same `searchadsorg` scope), so the
# Google-Sheets token relay is no longer needed. Sheets vars are still read as
# a fallback so an old deployment keeps working until it is updated.
ASA_CLIENT_ID = os.environ.get("ASA_CLIENT_ID", "")
ASA_TEAM_ID = os.environ.get("ASA_TEAM_ID", "")
ASA_KEY_ID = os.environ.get("ASA_KEY_ID", "")
ASA_PRIVATE_KEY_PEM = os.environ.get("ASA_PRIVATE_KEY_PEM", "")
ASA_V1_BASE = "https://api.ads.apple.com/v1"
ASA_V1_ENABLED = bool(ASA_CLIENT_ID and ASA_TEAM_ID and ASA_KEY_ID and ASA_PRIVATE_KEY_PEM)

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

# Read customers from the SQLite fact store instead of walking the RC API.
# Set STORE_MODE=0 to force the old path (useful to A/B the two outputs).
STORE_MODE = os.environ.get("STORE_MODE", "1") != "0"

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

def sign_with_openssl(data: bytes, key_path: str) -> bytes:
    digest = hashlib.sha256(data).digest()
    result = subprocess.run(
        ["openssl", "pkeyutl", "-sign", "-inkey", key_path,
         "-pkeyopt", "digest:sha256"],
        input=digest,
        capture_output=True,
        check=True,
    )
    der_sig = result.stdout
    assert der_sig[0] == 0x30
    assert der_sig[2] == 0x02
    r_len = der_sig[3]
    r_bytes = der_sig[4:4 + r_len]
    s_start = 4 + r_len
    assert der_sig[s_start] == 0x02
    s_len = der_sig[s_start + 1]
    s_bytes = der_sig[s_start + 2:s_start + 2 + s_len]
    r_padded = r_bytes.lstrip(b"\x00").rjust(32, b"\x00")
    s_padded = s_bytes.lstrip(b"\x00").rjust(32, b"\x00")
    return r_padded + s_padded


def _asa_v1_client_secret() -> str:
    """Build the ES256 client-secret JWT. Same shape refresh_token.py uses."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(ASA_PRIVATE_KEY_PEM.replace("\\n", "\n"))
        key_path = f.name
    try:
        now = int(time.time())
        header = {"alg": "ES256", "kid": ASA_KEY_ID}
        payload = {
            "sub": ASA_CLIENT_ID,
            "aud": "https://appleid.apple.com",
            "iat": now,
            "exp": now + 86400 * 180,
            "iss": ASA_TEAM_ID,
        }
        h = base64url_encode(json.dumps(header, separators=(",", ":")).encode())
        pl = base64url_encode(json.dumps(payload, separators=(",", ":")).encode())
        sig = sign_with_openssl(f"{h}.{pl}".encode("ascii"), key_path)
        return f"{h}.{pl}.{base64url_encode(sig)}"
    finally:
        try:
            os.unlink(key_path)
        except OSError:
            pass


_ASA_V1_TOKEN = {"value": None, "expires_at": 0}


def asa_v1_token() -> str:
    """Fetch (and cache in-process) a v1 access token."""
    if _ASA_V1_TOKEN["value"] and time.time() < _ASA_V1_TOKEN["expires_at"] - 60:
        return _ASA_V1_TOKEN["value"]
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": ASA_CLIENT_ID,
        "client_secret": _asa_v1_client_secret(),
        "scope": "searchadsorg",
    }).encode()
    req = urllib.request.Request(
        "https://appleid.apple.com/auth/oauth2/token",
        data=data,
        headers={"Host": "appleid.apple.com",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    _ASA_V1_TOKEN["value"] = body["access_token"]
    _ASA_V1_TOKEN["expires_at"] = time.time() + int(body.get("expires_in", 3600))
    return _ASA_V1_TOKEN["value"]


def asa_v1(method: str, path: str, body: dict = None, retries: int = 3) -> dict:
    """Call the Apple Ads Platform API v1. Returns {} on failure — ASA data is
    additive to the dashboard and must never abort the RevenueCat refresh."""
    url = f"{ASA_V1_BASE}{path}"
    for attempt in range(retries):
        try:
            headers = {
                "Authorization": f"Bearer {asa_v1_token()}",
                "X-AP-Context": f"orgId={ORG_ID}",
                "Content-Type": "application/json",
            }
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # 2000, not 300: Apple's validation errors carry the allowed values
            # for the field that failed, and that list is exactly what turns a
            # rejection into a fix. Truncating at 300 cut it off mid-list and
            # cost another round trip to learn what it already told us.
            detail = e.read().decode()[:2000]
            if e.code == 401 and attempt < retries - 1:
                _ASA_V1_TOKEN["value"] = None      # force refresh, retry
                continue
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            print(f"  ASA v1 {method} {path} -> {e.code}: {detail}")
            return {}
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            print(f"  ASA v1 {method} {path} failed: {e}")
            return {}
    return {}


def asa_v1_paged(path: str, body: dict, page_size: int = 1000) -> list:
    """POST a /query endpoint and follow pagination to the end."""
    rows, offset = [], 0
    while True:
        payload = dict(body)
        payload["pagination"] = {"offset": offset, "pageSize": page_size}
        resp = asa_v1("POST", path, payload)
        # The management endpoints (/campaigns/query, /adgroups/query) return
        # their rows under "result"; the reporting endpoints use "data". We
        # only ever read "data", so every campaign fetch came back empty and
        # reported "0 campaigns" with nothing to explain it.
        data = resp.get("data")
        if data is None:
            data = resp.get("result")
        data = data or []
        if isinstance(data, dict):
            # Reporting endpoints nest rows under reportingDataResponse; the
            # management endpoints return a bare list. Try each known shape.
            data = (data.get("rows") or data.get("results")
                    or (data.get("reportingDataResponse") or {}).get("row") or [])
        if offset == 0 and not data:
            # Returning [] silently here is how "0 campaigns" appeared with no
            # explanation. Surface what actually came back so the next run
            # diagnoses itself instead of needing another round trip.
            print(f"  ASA v1 {path}: no rows. response keys="
                  f"{sorted(resp.keys())} data type={type(resp.get('data')).__name__}"
                  + (f" data keys={sorted(resp['data'].keys())}"
                     if isinstance(resp.get("data"), dict) else ""))
        rows.extend(data)
        pg = (resp.get("pagination") or {})
        # v1 reports totalCount; v5 called it totalResults. Reading only the
        # old name meant the early-exit never fired and paging relied purely
        # on a short page.
        total = pg.get("totalCount", pg.get("totalResults"))
        if not data:
            break
        # "shorter page means last page" is only true if the server honoured
        # the page size we asked for. The popularity feed caps a page at 500
        # however much you request, so asking for 1000 made every first page
        # look like the last one and silently truncated the feed to its top
        # 500 terms per market. Trust the server's own page size.
        served = pg.get("pageSize") or page_size
        if len(data) < min(page_size, served):
            break
        offset += page_size
        if total is not None and offset >= total:
            break
    return rows


def asa_v1_report(entity: str, start: str, end: str,
                  group_by: list = None, extra_filters: list = None,
                  campaign_id=None) -> list:
    """Reporting for APPS. entity: campaigns | adgroups | keywords | ads | searchterms.

    Keyword and search-term reports are still PER CAMPAIGN in v1, exactly as
    in v5 -- omitting the filter fails validation with "campaignId filter is
    required for KEYWORD reports when promotedObjectType is APPS". Callers
    should use asa_v1_report_all_campaigns() rather than handling that here.

    SEARCHTERM does not accept UTC, so it uses the org timezone.
    """
    body = {
        "timeRange": {
            "start": start,
            "end": end,
            "timeZone": "ORTZ" if entity == "searchterms" else "UTC",
            "granularity": "DAILY",
        },
        # v1 renamed the sort order values: v5 accepted DESCENDING, v1 rejects
        # it with VALIDATION_ERROR and takes only ASC/DESC.
        "sorting": [{"field": "localSpend", "order": "DESC"}],
    }
    if group_by:
        body["groupBy"] = group_by
    filters = list(extra_filters or [])
    if campaign_id is not None:
        # SINGULAR "value". A "values" array is rejected with the misleading
        # "Filter value required. Field: campaignId" -- the field is named in
        # the error, so it reads like the id is missing rather than the key
        # being wrong. campaignId also only accepts EQUALS, never IN.
        filters.append({"field": "campaignId", "operator": "EQUALS",
                        "value": str(campaign_id)})
    if filters:
        body["filters"] = filters
    # EMPTY_METRICS is rejected alongside a campaignId filter, and it is only
    # there to surface keywords that spent nothing -- which a per-campaign
    # report already covers.
    if entity not in ("searchterms", "keywords"):
        body["options"] = {"includeRows": ["EMPTY_METRICS"]}
    return asa_v1_paged(f"/reports/apps/{entity}/query", body)


def asa_v1_report_all_campaigns(entity: str, start: str, end: str,
                                campaign_ids, group_by: list = None) -> list:
    """Run a per-campaign report across every campaign and concatenate.

    One campaign failing must not lose the others, so failures are reported
    and skipped rather than raised.
    """
    rows = []
    for cid in campaign_ids:
        try:
            rows.extend(asa_v1_report(entity, start, end, group_by=group_by,
                                      campaign_id=cid) or [])
        except Exception as e:
            print(f"  ASA v1 {entity} report failed for campaign {cid}: "
                  f"{type(e).__name__}: {e}")
    return rows


def asa_v1_keyword_popularity(keywords: list, country: str = "US",
                              month_start: str = None,
                              month_end: str = None) -> dict:
    """Apple's search-volume score per term.

    Returns {term_lower: {"popularity", "rank_in_genre", "genre", "month"}}.

    This is the field that makes "does volume predict profit?" answerable: it
    lands on the same row as revenue and cost-per-sub in data.json.

    The endpoint's contract, established by probing it directly:
      * timeRange is REQUIRED, and its granularity must be MONTHLY or
        WEEKLY_SUN_SAT -- DAILY is rejected.
      * Filters use a SINGULAR "value"; a "values" array comes back as
        "Unrecognized property 'values'", so terms cannot be batched and it is
        one request per term per country.
      * Unfiltered, it returns the most-searched terms per country and genre,
        which makes it a keyword DISCOVERY feed as well as a lookup.

    searchPopularity1to100 is the useful number: Apple's 1-100 volume score.
    A term Apple does not rank simply returns no row, and is recorded as None
    rather than 0 -- "unknown" and "nobody searches this" are different facts.
    """
    if not keywords:
        return {}

    if not month_start or not month_end:
        today = datetime.now(timezone.utc).date()
        # Apple publishes this monthly and the current month is incomplete, so
        # look back far enough to always cover a settled one.
        month_start = (today - timedelta(days=60)).replace(day=1).isoformat()
        month_end = today.isoformat()

    out = {}
    for term in keywords:
        term = (term or "").strip()
        if not term:
            continue
        resp = asa_v1("POST", "/insights/apps/search-term-popularity/query", {
            "timeRange": {"start": month_start, "end": month_end,
                          "timeZone": "UTC", "granularity": "MONTHLY"},
            "filters": [
                {"field": "countryOrRegion", "operator": "EQUALS", "value": country},
                {"field": "searchTerm", "operator": "EQUALS", "value": term},
            ],
            "pagination": {"offset": 0, "pageSize": 12},
        })
        rows = ((resp.get("result") or {}).get("rows")
                if isinstance(resp.get("result"), dict) else None) or []
        if not rows:
            continue
        # Several months may come back; keep the most recent.
        row = max(rows, key=lambda r: r.get("month") or "")
        pop = row.get("searchPopularity1to100")
        if pop is None:
            continue
        out[term.lower()] = {
            "popularity": int(pop),
            "rank_in_genre": row.get("rankInGenre"),
            "genre": row.get("genre"),
            "month": row.get("month"),
        }

    print(f"  ASA v1: popularity resolved for {len(out)}/{len(keywords)} terms "
          f"in {country}")
    return out


def asa_v1_top_search_terms(country: str = "US", genre: str = None,
                            limit: int = 200) -> list:
    """The unfiltered popularity feed: Apple's most-searched terms.

    Same endpoint with no searchTerm filter, which turns it into keyword
    discovery -- what people in a market actually search, ranked, with a
    volume score. Useful for building a keyword list rather than guessing one.
    """
    today = datetime.now(timezone.utc).date()
    body = {
        "timeRange": {
            "start": (today - timedelta(days=60)).replace(day=1).isoformat(),
            "end": today.isoformat(),
            "timeZone": "UTC", "granularity": "MONTHLY",
        },
        "filters": [{"field": "countryOrRegion", "operator": "EQUALS",
                     "value": country}],
        "pagination": {"offset": 0, "pageSize": min(int(limit), 1000)},
    }
    if genre:
        body["filters"].append({"field": "genre", "operator": "EQUALS",
                                "value": genre})
    resp = asa_v1("POST", "/insights/apps/search-term-popularity/query", body)
    rows = ((resp.get("result") or {}).get("rows")
            if isinstance(resp.get("result"), dict) else None) or []
    return rows


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
    #
    # Step 4 added rotation: when the live file crosses 10 MB, the webhook
    # receiver atomically renames it to rc_events-YYYYMMDD-HHMMSS.jsonl.archive
    # so the file stays small. The reader walks ALL rotated archives
    # oldest-first then the live file, so the data stream is uninterrupted
    # across rotations. Events older than since_ms in the archives are
    # skipped at parse time.
    events = None
    skipped_count = 0
    if LOCAL_OUTPUT_DIR:
        import glob as _glob
        live_path = os.path.join(LOCAL_OUTPUT_DIR, "rc_events.jsonl")
        archive_pattern = os.path.join(LOCAL_OUTPUT_DIR, "rc_events-*.jsonl.archive")
        archive_files = sorted(_glob.glob(archive_pattern))  # chronological
        log_files = list(archive_files)
        if os.path.exists(live_path):
            log_files.append(live_path)

        if log_files:
            try:
                events = []
                for path in log_files:
                    with open(path, "r") as f:
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
                file_summary = (
                    f"live + {len(archive_files)} archive(s)"
                    if archive_files else "live only"
                )
                print(f"  [webhooks] read {len(events)} events from local "
                      f"({file_summary}, skipped {skipped_count} older than 60d)")
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

    # ── Only enrich customers who can appear in a revenue table ─────────────
    # The revenue/channel/campaign/keyword tables are built exclusively from
    # customers with transactions. Enriching everyone meant 2 RevenueCat calls
    # for every person who ever opened the app -- hundreds of thousands who
    # paid nothing -- so runtime tracked total install history rather than
    # revenue activity: 15 min in May, 4+ hours by August, at which point runs
    # outlasted their own hourly trigger and data.json silently froze.
    #
    # The webhook log already identifies everyone who transacted, and it keeps
    # 60 days while the widest reporting window is 30 -- so it fully covers
    # every range the dashboard reports, with 30 days of headroom.
    #
    # Customers with no transactions are given explicit zeroed fields rather
    # than being dropped, so cohort/retention and totals still see the full
    # population; they simply cost no API calls.
    # The insurance window below is deliberately SHORT. It was first written as
    # 60 days -- matching the webhook window -- which enriched every install of
    # the last 60 days and defeated the whole point: the Aug 19 run enriched
    # 73,401 of 109,917 customers (66.8%) and 62,044 of those came back with no
    # subscription at all. Those 62k are ~124k round trips spent confirming that
    # non-payers did not pay.
    #
    # Webhooks arrive within seconds of a purchase, so the only payers the log
    # can miss are ones bought during a receiver outage. Two days covers that
    # and costs ~2 days of installs instead of 60.
    INSURANCE_DAYS = 2
    recent_cutoff_ms = (
        datetime.now(timezone.utc) - timedelta(days=INSURANCE_DAYS)
    ).timestamp() * 1000

    def _seen_recently(c) -> bool:
        """Installed within INSURANCE_DAYS -- may have paid during a webhook
        receiver outage. Cheap insurance against undercounting brand-new
        payers, bounded so it cannot dominate the run."""
        fs = c.get("first_seen_at")
        if not fs:
            return False
        try:
            if isinstance(fs, (int, float)):
                return float(fs) >= recent_cutoff_ms
            return datetime.fromisoformat(
                str(fs).replace("Z", "+00:00")
            ).timestamp() * 1000 >= recent_cutoff_ms
        except Exception:
            return True          # unparseable -> enrich, never silently skip

    to_enrich, skipped = [], []
    for c in customers:
        if c["id"] in webhook_events or _seen_recently(c):
            to_enrich.append(c)
        else:
            skipped.append(c)

    for c in skipped:
        c["_attrs"] = {}
        c["_revenue"] = 0.0
        c["_active"] = False
        c["_canceled"] = False
        c["_renewals"] = 0
        c["_tier"] = "none"
        c["_tier_counts"] = {}
        c["_tier_revenue"] = {}
        c["_sub_count"] = 0
        c["_transactions"] = []
        c["_txn_source"] = "none"

    pct = (len(to_enrich) / len(customers) * 100) if customers else 0
    print(f"  Enriching {len(to_enrich)} customers "
          f"({pct:.1f}% of {len(customers)}): {len(webhook_events)} from the "
          f"webhook log + installs from the last {INSURANCE_DAYS}d; "
          f"{len(skipped)} skipped -> zero API calls")

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
            c = future.result()
            c["_cached_at"] = datetime.now(timezone.utc).isoformat()
            enriched.append(c)
            done += 1
            if done % 100 == 0:
                print(f"    {done}/{len(to_enrich)}")

    # Non-transacting customers carry zeroed fields — fold them back so
    # cohort/retention and population counts still see everyone.
    enriched.extend(skipped)

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


def fetch_rc_authoritative_revenue(days: int = 31) -> dict:
    """Pull per-day revenue from RC's /v2/.../metrics/revenue endpoint.

    The webhook-log + inference path we use elsewhere can undercount when
    RC's webhook delivery was failing (we saw this with the May 16 → Jun 19
    Basic-Auth outage — even after RC retried, some events were dropped
    after retention expired). This endpoint returns the same number RC's
    UI shows on its Revenue chart, so it's authoritative.

    Strategy: query one day at a time so we get a per-day dict
    {date_iso: usd_value}. RC accepts a date range and returns the SUM,
    not a series, so 30 calls is the simplest correct way. Cheap because
    each call is ~150-300 ms and we run them in 8 parallel workers.

    Returns {} on any failure so the caller can fall back to the
    webhook-derived value (we never want the full refresh to abort
    because of this enrichment step).
    """
    if not REVENUECAT_API_KEY:
        return {}

    from concurrent.futures import ThreadPoolExecutor, as_completed
    today = datetime.now(timezone.utc).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(days)]

    def fetch_one(d):
        url = (
            f"https://api.revenuecat.com/v2/projects/"
            f"{REVENUECAT_PROJECT_ID}/metrics/revenue"
            f"?start_date={d}&end_date={d}"
        )
        try:
            data = _rc_get_json(url, timeout=20, max_retries=2)
            return d, float(data.get("value") or 0)
        except Exception as e:
            print(f"  [rc-revenue] {d}: skipped ({type(e).__name__})")
            return d, None

    result = {}
    with ThreadPoolExecutor(max_workers=8) as exe:
        for fut in as_completed([exe.submit(fetch_one, d) for d in dates]):
            d, v = fut.result()
            if v is not None:
                result[d] = v
    total = sum(result.values())
    print(
        f"  [rc-revenue] fetched authoritative daily revenue for "
        f"{len(result)}/{days} days, sum ${total:,.2f}"
    )
    return result


def apply_authoritative_revenue(daily_rc, authoritative):
    """Overwrite each daily_rc entry's `revenue` with RC's authoritative
    per-day value. Leaves new_subs / renewals / tier counts alone (those
    still come from webhook events and are useful for the dashboard's
    breakdown columns).

    Days where the authoritative fetch failed keep their webhook-derived
    value so we never zero out good data on a transient API error."""
    if not authoritative:
        return daily_rc
    for entry in daily_rc:
        d = entry.get("date")
        if d in authoritative:
            entry["revenue"] = round(authoritative[d], 2)
    return daily_rc


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
# Pre-flight validation — refuse to publish obviously broken data
# ══════════════════════════════════════════════════════════════════

def validate_daily_rc(new_daily_rc, prev_daily_rc, *, source="unknown"):
    """
    Sanity-check a freshly computed daily_rc against the previous published
    version. Used by both the full and fast refresh paths to refuse writes
    that would silently corrupt the dashboard.

    Returns (is_ok: bool, reason: str).

    Rules (any one fails → reject):
      1. new_daily_rc must be a non-empty list
      2. Each entry must have the required fields
      3. For days >= 3 days old ("settled"), revenue MUST NOT drop more
         than 30% vs the previous version — settled days can have small
         refund-driven dips but not large drops
      4. For settled days where prev revenue was > $50, new revenue MUST
         NOT be exactly $0 — that pattern is what we just fought to fix
         (compute_daily_rc returning zeros after a webhook auth failure)

    First-run (no prev_daily_rc) passes through so a fresh setup isn't
    blocked. Days <= 3 days old are not compared because RC's webhook
    delivery lags ~24h and small revenue differences are expected.
    """
    if not isinstance(new_daily_rc, list) or len(new_daily_rc) == 0:
        return False, f"[{source}] daily_rc is empty or not a list"

    required_fields = {"date", "revenue", "new_subs", "renewals"}
    for i, entry in enumerate(new_daily_rc):
        if not isinstance(entry, dict):
            return False, f"[{source}] daily_rc[{i}] is not a dict"
        missing = required_fields - set(entry.keys())
        if missing:
            return False, (
                f"[{source}] daily_rc[{i}] (date={entry.get('date','?')}) "
                f"missing fields: {sorted(missing)}"
            )

    if not prev_daily_rc:
        return True, f"[{source}] no previous daily_rc — first-run, passing"

    prev_by_date = {e["date"]: e for e in prev_daily_rc if e.get("date")}
    today = datetime.now(timezone.utc).date()
    settled_cutoff = (today - timedelta(days=3)).isoformat()

    big_drops = []
    suspicious_zeros = []
    for entry in new_daily_rc:
        date = entry.get("date")
        if not date or date > settled_cutoff:
            continue
        prev_entry = prev_by_date.get(date)
        if not prev_entry:
            continue
        new_rev = float(entry.get("revenue") or 0)
        prev_rev = float(prev_entry.get("revenue") or 0)
        if prev_rev <= 0:
            continue
        if new_rev == 0 and prev_rev > 50:
            suspicious_zeros.append(f"{date}: new=$0, prev=${prev_rev:.0f}")
        elif new_rev < prev_rev * 0.70:
            pct = (new_rev / prev_rev - 1) * 100
            big_drops.append(f"{date}: ${prev_rev:.0f} → ${new_rev:.0f} ({pct:+.0f}%)")

    if suspicious_zeros:
        return False, (
            f"[{source}] settled days dropped to $0 (prev was non-zero): "
            + "; ".join(suspicious_zeros[:5])
        )
    if big_drops:
        return False, (
            f"[{source}] settled days dropped >30%: "
            + "; ".join(big_drops[:5])
        )

    return True, (
        f"[{source}] daily_rc validation passed "
        f"({len(new_daily_rc)} days, no settled-day regressions)"
    )


def load_existing_data_json():
    """Load the previously-published data.json so we can validate against it.
    Returns None if it doesn't exist or is unparseable — both are treated as
    'first run' by the validator."""
    if not LOCAL_OUTPUT_DIR:
        return None
    path = os.path.join(LOCAL_OUTPUT_DIR, "data.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"  [validate] could not read existing data.json: {e}")
        return None


def validate_full_data(new_data, prev_data):
    """
    Validate the FULL data.json output before publishing. Returns
    (is_ok, reason).

    Catches:
      - missing top-level fields
      - daily_rc failing validate_daily_rc()
      - total JSON size shrinking >50% vs previous (which would mean
        most arrays are empty — typically a fetch failure)
    """
    if not isinstance(new_data, dict):
        return False, "new data is not a dict"
    if "last_updated" not in new_data:
        return False, "missing last_updated field"

    for field in ("campaigns", "channels"):
        if field in new_data and not isinstance(new_data[field], list):
            return False, f"{field} is not a list"

    new_daily = new_data.get("daily_rc") or []
    prev_daily = (prev_data or {}).get("daily_rc") or []
    ok, reason = validate_daily_rc(new_daily, prev_daily, source="full-refresh")
    if not ok:
        return False, reason

    if prev_data:
        new_size = len(json.dumps(new_data, separators=(",", ":")))
        prev_size = len(json.dumps(prev_data, separators=(",", ":")))
        if new_size < prev_size * 0.5 and prev_size > 1000:
            return False, f"new size {new_size:,} < 50% of prev {prev_size:,}"

    return True, reason  # includes the daily_rc validation passed message


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

        # Retry the CONNECT. genivox.com intermittently refuses FTP outright —
        # "TimeoutError: timed out" raised from socket.connect, after the whole
        # refresh has already run. The sibling scripts' upload_to_ftp got this
        # guard; this copy did not, so Refresh Dashboard kept discarding 15-20
        # minutes of work at the last step.
        ftp = None
        for attempt in range(4):
            try:
                ftp = ftplib.FTP_TLS(FTP_HOST, timeout=90, context=ctx)
                ftp.login(FTP_USER, FTP_PASS)
                ftp.prot_p()
                break
            except Exception as e:
                print(f"    FTP connect attempt {attempt + 1}/4 failed: {e}")
                if attempt == 3:
                    raise
                time.sleep(20 * (attempt + 1))

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

    # Prefer Apple Ads Platform API v1: it mints its own token, so the old
    # Google-Sheets relay (which expired constantly and took ASA + RC data down
    # with it) is not involved. Falls back to the v5/Sheets path if the v1
    # credentials are not configured. v5 retires 2027-01-26.
    use_v1 = ASA_V1_ENABLED
    asa_token = None
    if use_v1:
        try:
            asa_v1_token()
            print("ASA: using Apple Ads Platform API v1")
        except Exception as e:
            print(f"⚠ ASA v1 auth failed ({e}) — falling back to v5.")
            use_v1 = False
    if not use_v1:
        if ASA_ENABLED:
            google_token = get_google_access_token()
            config = sheets_read(google_token, "_Config!B1:B1")
            if not config or not config[0]:
                print("⚠ ASA token cell empty — running RC-only.")
                asa_token = None
            else:
                asa_token = config[0][0]
                print("ASA: using legacy Campaign Management API v5 (retires 2027-01-26)")
        else:
            print("⚠ No ASA credentials (v1 or Sheets) — skipping ASA fetch, running RC-only.")
            asa_token = None

    if use_v1:
        print("Fetching campaigns (v1)...")
        campaigns = asa_v1_paged("/campaigns/query", {})
        print(f"Found {len(campaigns)} campaigns")
    elif asa_token:
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

    # ── Apple structure and spend from the fact store ────────────────────
    # Only RevenueCat was wired to the store; Apple was still fetched live at
    # build time. When that fetch came back empty the builder produced a
    # data.json with 0 campaigns and 0 keywords, the pre-flight size check
    # refused to publish it, and the dashboard silently sat on 06:30 data --
    # the check did its job, but the cause was here.
    #
    # The store already holds campaigns, ad groups and keywords from the
    # management API, which list them regardless of spend. Reading them here
    # means a paused account shows its structure instead of looking empty.
    if STORE_MODE:
        try:
            import store as _s
            _sc = _s.open_store()
            _store_camps = list(_sc.execute(
                "SELECT campaign_id, name, status, country, daily_budget "
                "FROM campaign_dim WHERE source='apple'"))
            # Replace, do not merge. The legacy fetch may already have filled
            # campaign_meta with the same campaigns under integer ids while the
            # store uses strings, so merging produced two rows per campaign --
            # 38 rows for 19 campaigns, every name duplicated.
            if _store_camps:
                campaign_meta.clear()
            for _c in _store_camps:
                campaign_meta[_c["campaign_id"]] = {
                    "id": _c["campaign_id"],
                    "name": _c["name"] or "",
                    "status": _c["status"] or "",
                    "countries": _c["country"] or "",
                    "budget": ("" if _c["daily_budget"] is None
                               else str(_c["daily_budget"])),
                }
            _kw_meta = {
                r["keyword_id"]: dict(r)
                for r in _sc.execute(
                    "SELECT keyword_id, campaign_id, adgroup_id, text, match_type, "
                    "       bid, popularity, rank_in_genre, genre, status "
                    "FROM keyword_dim")
            }
            _kw_text = {k: (v.get("text") or "").strip().lower()
                        for k, v in _kw_meta.items()}

            def _kw_fields(kid):
                """The display fields the keyword table reads off each row."""
                m = _kw_meta.get(kid, {})
                return {
                    "keyword": m.get("text") or "",
                    "match": m.get("match_type") or "",
                    "status": m.get("status") or "",
                    "bid": m.get("bid") or 0,
                    "is": "",
                    "ad_group_id": m.get("adgroup_id") or None,
                    "keyword_id": kid,
                    "popularity": m.get("popularity"),
                    "rank_in_genre": m.get("rank_in_genre"),
                    "genre": m.get("genre"),
                }

            # Spend per range, summed from the daily facts. A range with no
            # rows correctly contributes nothing rather than erroring.
            for _r, (_start, _end) in ranges.items():
                for _row in _sc.execute(
                    "SELECT campaign_id, keyword_id, "
                    "       ROUND(SUM(spend),2) spend, SUM(impressions) impr, "
                    "       SUM(taps) taps, SUM(installs) inst "
                    "FROM ad_daily WHERE source='apple' AND day BETWEEN ? AND ? "
                    "GROUP BY campaign_id, keyword_id",
                    (_start.isoformat(), _end.isoformat()),
                ):
                    _base = {
                        "spend": float(_row["spend"] or 0),
                        "impressions": int(_row["impr"] or 0),
                        "taps": int(_row["taps"] or 0),
                        "installs": int(_row["inst"] or 0),
                        "cpt": 0.0, "cpa": 0.0, "ttr": 0.0,
                    }
                    if _row["taps"]:
                        _base["cpt"] = round(_base["spend"] / _row["taps"], 4)
                        if _row["impr"]:
                            _base["ttr"] = round(_row["taps"] / _row["impr"] * 100, 4)
                    if _row["inst"]:
                        _base["cpa"] = round(_base["spend"] / _row["inst"], 4)

                    _cid = _row["campaign_id"]
                    if _row["keyword_id"]:
                        _kw = _kw_text.get(_row["keyword_id"])
                        if not _kw:
                            continue
                        _base.update(_kw_fields(_row["keyword_id"]))
                        asa_keyword_data[_r][(_cid, _kw)] = _base
                    else:
                        # setdefault() inserts a COPY, so the value it hands
                        # back is never the same object as _base -- the old
                        # "is not _base" guard was therefore always true and
                        # every campaign got its first row added twice, on top
                        # of the copy that already held it. That is why the
                        # dashboard read exactly 2.00x Apple's spend.
                        _agg = asa_campaign_data[_r].get(_cid)
                        if _agg is None:
                            asa_campaign_data[_r][_cid] = dict(_base)
                        else:
                            for _k in ("spend", "impressions", "taps", "installs"):
                                _agg[_k] += _base[_k]
                # Campaign totals roll up from keyword rows when Apple reported
                # at keyword grain only, so the campaign table is never blank
                # while its keywords have spend.
                _by_camp = {}
                for (_cid, _kw), _d in asa_keyword_data[_r].items():
                    _by_camp.setdefault(_cid, []).append(_d)
                for _cid, _kws in _by_camp.items():
                    if _cid in asa_campaign_data[_r]:
                        continue
                    asa_campaign_data[_r][_cid] = {
                        "spend": round(sum(k["spend"] for k in _kws), 2),
                        "impressions": sum(k["impressions"] for k in _kws),
                        "taps": sum(k["taps"] for k in _kws),
                        "installs": sum(k["installs"] for k in _kws),
                        "cpt": 0.0, "cpa": 0.0, "ttr": 0.0,
                    }

            # Every keyword we target gets an "all" entry even with no spend,
            # so a paused account still shows its keyword list with search
            # volume attached -- which is the whole point of holding structure
            # separately from metrics.
            for _kid, _m in _kw_meta.items():
                _kw = (_m.get("text") or "").strip().lower()
                if not _kw:
                    continue
                _key = (_m.get("campaign_id"), _kw)
                if _key not in asa_keyword_data["all"]:
                    asa_keyword_data["all"][_key] = dict(
                        spend=0.0, impressions=0, taps=0, installs=0,
                        cpt=0.0, cpa=0.0, ttr=0.0, **_kw_fields(_kid))
            print(f"  [store] Apple: {len(campaign_meta)} campaigns, "
                  f"{len(_kw_meta)} keywords, "
                  f"{sum(1 for m in _kw_meta.values() if m.get('popularity'))} with volume")
        except Exception as e:
            print(f"  [store] Apple structure unavailable ({type(e).__name__}: {e})")
            _kw_meta = {}
    else:
        _kw_meta = {}

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

    # ── RevenueCat ───────────────────────────────────────────────────────
    # STORE_MODE reads customers from the local SQLite fact store, which
    # ingest_rc.py fills from the webhook log in well under a second. The
    # legacy path below walks all ~111k customers over the REST API and takes
    # 20-70 minutes, growing with total install history rather than with
    # activity -- that is what outran the cron and froze the dashboard.
    #
    # The store returns the identical customer-dict shape, so everything
    # downstream (revenue index, daily_rc, cohorts, every column) is unchanged.
    # Falls back to the API path if the store is missing or empty, so a host
    # that has not been backfilled yet still produces a correct dashboard.
    customers = None
    if STORE_MODE:
        try:
            import store as _store
            import store_adapter as _adapter
            _conn = _store.open_store()
            _st = _store.store_stats(_conn)
            if _st["txn"]:
                print("\n--- RevenueCat (fact store) ---")
                _t0 = time.time()
                customers = _adapter.customers_from_store(_conn)
                _a = _adapter.adapter_stats(customers)
                print(f"  {_a['customers']} customers, {_a['paying']} paying, "
                      f"{_a['active']} active  ({time.time()-_t0:.1f}s, zero API calls)")
                print(f"  gross ${_a['gross']:,.2f} -> proceeds ${_a['proceeds']:,.2f} "
                      f"({_a['proceeds']/_a['gross']*100:.1f}% take-home)"
                      if _a["gross"] else "  no revenue in store")
            else:
                print("\n  [store] empty -- falling back to the API walk")
        except Exception as e:
            print(f"\n  [store] unavailable ({type(e).__name__}: {e}) -- falling back to the API walk")
            customers = None

    if customers is None:
        print("\n--- Fetching RevenueCat (legacy API walk) ---")
        customers = rc_get_all_customers()
        customers = rc_enrich_customers(customers)

    rev_index = build_revenue_index(customers)
    # 31 days = today + 30 days back, so a "last 30 days excluding today"
    # view on the dashboard has all the data it needs.
    daily_rc = compute_daily_rc(customers, days=31)
    print(f"  Daily RC (webhook-derived): {len(daily_rc)} days, "
          f"latest revenue ${daily_rc[-1]['revenue']:.2f}")

    # Overwrite the webhook-derived revenue with RC's authoritative
    # per-day metrics. This matches what RC's own UI shows on the Revenue
    # chart, and it eliminates the silent undercount we get when webhook
    # delivery has been intermittent (the May 16 → Jun 19 outage was
    # leaving ~$200-300/day off the dashboard).
    authoritative = fetch_rc_authoritative_revenue(days=31)
    apply_authoritative_revenue(daily_rc, authoritative)
    print(f"  Daily RC (authoritative): latest revenue ${daily_rc[-1]['revenue']:.2f}")
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

            _sp = row[f"spend_{r}"]
            _inst = row[f"installs_{r}"]
            _subs = rev_data.get("paid_subs", 0)
            _rev = rev_data.get("revenue", 0)
            row[f"profit_{r}"] = round(_rev * 0.85 - _sp, 2)
            row[f"inst_to_sub_{r}"] = round(_subs / _inst * 100, 1) if _inst else None
            row[f"rev_per_install_{r}"] = round(_rev / _inst, 2) if _inst else None
            row[f"cpa_{r}"] = round(_sp / _subs, 2) if _subs else None
            row[f"roas_{r}"] = round(_rev / _sp * 100) if _sp else None
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

    # ── Search terms: what people ACTUALLY typed ───────────────────────────
    # Distinct from keywords, which is what you bid on. Two uses: spot spend on
    # terms you never wanted (-> negatives), and spot terms converting well
    # that you are not bidding on yet (-> promote to exact). No v5 equivalent.
    searchterms_out = []
    if use_v1 and campaign_meta:
        try:
            for r, (rs, re_) in ranges.items():
                # Apple rejects DAILY granularity beyond ~90 days, so the "all"
                # range (from 2026-03-01) fails outright. Report the widest
                # window it will accept instead of losing the range entirely.
                if (re_ - rs).days > 89:
                    rs = re_ - timedelta(days=89)
                # SEARCHTERM reports require a campaignId filter exactly as
                # KEYWORD reports do; without it every call 400s and the retry
                # loop turns a fast build into a slow one.
                for row_ in asa_v1_report_all_campaigns(
                        "searchterms", rs.isoformat(), re_.isoformat(),
                        list(campaign_meta.keys())):
                    m = row_.get("metadata", {}) or {}
                    t = row_.get("total", {}) or {}
                    term = (m.get("searchTermText") or m.get("searchTerm") or "").strip()
                    if not term:
                        continue
                    searchterms_out.append({
                        "range": r,
                        "search_term": term,
                        "keyword": m.get("keyword", ""),
                        "match": m.get("matchType", ""),
                        "campaign_id": m.get("campaignId"),
                        "ad_group_id": m.get("adGroupId"),
                        "spend": round(float((t.get("localSpend") or {}).get("amount", 0) or 0), 2),
                        "impressions": int(t.get("impressions", 0) or 0),
                        "taps": int(t.get("taps", 0) or 0),
                        "installs": int(t.get("totalInstalls", 0) or 0),
                    })
            print(f"  ASA v1: {len(searchterms_out)} search-term rows")
        except Exception as e:
            print(f"  search-terms fetch skipped: {e}")

    # ── Apple's own recommendations ────────────────────────────────────────
    recommendations_out = []
    # Apple requires filters on promotedObjectId AND promotedObjectType:
    #   "REQUIRED_VALUE_FIELD filters.promotedObjectId ... filters.promotedObjectType"
    # promotedObjectId is the App Store app id, which every campaign carries.
    _promoted_id = os.environ.get("ASA_APP_ID", "6758404269")
    if use_v1 and _promoted_id:
        _rec_body = {
            "filters": [
                {"field": "promotedObjectId", "operator": "EQUALS",
                 "value": str(_promoted_id)},
                {"field": "promotedObjectType", "operator": "EQUALS",
                 "value": "APPSTORE_APP"},
            ],
            "pagination": {"offset": 0, "pageSize": 200},
        }
        for kind, path in (("daily_budget", "/recommendations/daily-budgets/query"),
                           ("target_cpa", "/recommendations/target-cpas/query")):
            try:
                for rec in asa_v1_paged(path, _rec_body):
                    rec["_kind"] = kind
                    recommendations_out.append(rec)
            except Exception as e:
                print(f"  {kind} recommendations skipped: {e}")
        if recommendations_out:
            print(f"  ASA v1: {len(recommendations_out)} recommendations")

    # Keywords list — merge across ranges by (campaign_id, keyword)
    kw_union = set()
    for r_data in asa_keyword_data.values():
        for key in r_data:
            kw_union.add(key)

    # Apple's search-volume score per term (v1 only). Landing it on the same
    # row as revenue/cost-per-sub is what makes "does volume predict profit?"
    # answerable directly from data.json instead of by eye.
    keyword_popularity = {}
    if use_v1:
        try:
            terms = sorted({kw for (_cid, kw) in kw_union if kw})
            keyword_popularity = asa_v1_keyword_popularity(terms)
        except Exception as e:
            print(f"  popularity lookup skipped: {e}")

    def _volume_of(country, term, live_lookup):
        """(popularity, rank, genre) for a term IN ITS OWN MARKET.

        Falls back to the build-time lookup only when the market-specific feed
        has nothing, and unwraps it because it returns a dict per term rather
        than a bare number. None means Apple does not rank the term, which is
        different from zero volume and must stay distinguishable.
        """
        hit = _vol.get((country, term))
        if hit:
            return hit
        # The build-time lookup queries Apple with country="US", so its answer
        # is only valid for the US. Using it elsewhere is what showed AU and CA
        # the US score of 63 at rank 208 while GB, which the feed did cover,
        # correctly showed 57 at rank 201.
        #
        # A term absent from its own market's feed means Apple does not rank it
        # there. Reporting None is the honest answer; substituting another
        # country's demand produces a number that looks researched and is not.
        if country != "US":
            return (None, None, None)
        live = live_lookup.get(term)
        if isinstance(live, dict):
            return (live.get("popularity"), live.get("rank_in_genre"),
                    live.get("genre"))
        return (live, None, None)

    # Volume per (country, term). Keying by term alone let one market's number
    # stand in for every other: "instagram unfollowers" showed the US score of
    # 63 (rank 208) in AU, CA and GB, where Britain's actual figure is 57
    # (rank 201). Search volume is a property of a market, so the market has to
    # be part of the key.
    _vol = {}
    if STORE_MODE:
        try:
            import store as _s2
            for _r in _s2.open_store().execute(
                "SELECT country, LOWER(term) t, popularity, rank_in_genre, genre "
                "FROM search_term_popularity "
                "WHERE month = (SELECT MAX(month) FROM search_term_popularity)"
            ):
                _vol[(_r["country"], _r["t"])] = (
                    _r["popularity"], _r["rank_in_genre"], _r["genre"])
            print(f"  volume lookup: {len(_vol)} (market, term) pairs")
        except Exception as e:
            print(f"  [store] volume lookup unavailable ({type(e).__name__})")

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

        _vol_pop, _vol_rank, _vol_genre = _volume_of(country, kw_lower,
                                                     keyword_popularity)
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
            # Apple search-volume score. None when unavailable (v5, or term not
            # resolved) — the dashboard should treat None as "unknown", not 0.
            # Prefer the store's value: it was resolved against the keyword's
            # OWN market and joined to Apple's volume feed, where the
            # build-time lookup assumes US. None means "Apple does not rank
            # this term", never zero volume.
            "popularity": _vol_pop,
            "rank_in_genre": _vol_rank,
            "genre": _vol_genre,
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

            # Derived decision metrics. Arithmetic on columns already present,
            # but computing them here means the dashboard never has to and the
            # same definition is used everywhere.
            _sp = row[f"spend_{r}"]
            _inst = row[f"installs_{r}"]
            _subs = rev_data.get("paid_subs", 0)
            _rev = rev_data.get("revenue", 0)
            # Profit in dollars after Apple's cut — ROAS tells you efficiency,
            # dollars tell you what is actually worth scaling.
            row[f"profit_{r}"] = round(_rev * 0.85 - _sp, 2)
            # The number that separates a keyword buying cheap junk traffic
            # from one buying buyers.
            row[f"inst_to_sub_{r}"] = round(_subs / _inst * 100, 1) if _inst else None
            # Comparable across keywords with very different volumes.
            row[f"rev_per_install_{r}"] = round(_rev / _inst, 2) if _inst else None
            row[f"cpa_{r}"] = round(_sp / _subs, 2) if _subs else None
            row[f"roas_{r}"] = round(_rev / _sp * 100) if _sp else None
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

    # Apple's market-wide search volume, from the store. This is keyword
    # RESEARCH, not reporting: the most-searched terms per market whether or
    # not we bid on them, which is what makes "which keywords should we buy"
    # answerable without guessing.
    keyword_volume = []
    if STORE_MODE:
        try:
            import store as _s
            _c = _s.open_store()
            keyword_volume = [dict(r) for r in _c.execute(
                "SELECT country, genre, term, rank_in_genre, popularity, month "
                "FROM search_term_popularity "
                "WHERE month = (SELECT MAX(month) FROM search_term_popularity) "
                "ORDER BY country, genre, rank_in_genre LIMIT 20000")]
            if keyword_volume:
                print(f"  keyword volume feed: {len(keyword_volume)} ranked terms")
        except Exception as e:
            print(f"  [store] keyword volume unavailable ({type(e).__name__})")

    output = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "keyword_volume": keyword_volume,
        "campaigns": campaigns_out,
        "search_terms": searchterms_out,
        "recommendations": recommendations_out,
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

    # Pre-flight validation: refuse to publish data.json if it looks broken
    # (zero revenue on settled days, missing fields, dramatic size shrink).
    # The previous good data.json stays in place — the dashboard keeps
    # showing the last-known-good numbers until the next refresh succeeds.
    print("\n--- Pre-flight validation ---")
    prev_data = load_existing_data_json()
    ok, reason = validate_full_data(output, prev_data)
    print(f"  {reason}")
    if not ok:
        print(
            "\n❌ REFUSING TO PUBLISH — validation failed. "
            "Existing data.json left untouched. Investigate the source "
            "and re-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Upload
    print("\n--- Uploading to cPanel ---")
    publish_output(out_file, "data.json")

    print("\n🎉 Done!")


if __name__ == "__main__":
    main()
