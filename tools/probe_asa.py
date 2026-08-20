#!/usr/bin/env python3
"""
Dump the real shape of every Apple Ads API response we depend on.

WHY
---
The Apple Ads credentials are write-only GitHub secrets, so nothing here can
be called from a laptop. That turned every incorrect assumption about the v1
API into a full push -> dispatch -> wait -> read-log cycle, and we burned
three of them on guesses that a single dump would have settled:

  * management endpoints answer under "result", reporting under "data"
  * sort order is DESC, not v5's DESCENDING
  * keyword reports still require a campaignId filter, one call per campaign
  * EMPTY_METRICS is rejected alongside that filter

This script asks every endpoint we use, prints the envelope keys, the row
field names and one real sample per endpoint, and keeps going after failures
so one broken call cannot hide the rest. Run it once, write code against what
it prints, deploy once.

Run via the "Probe Apple Ads API" workflow, or locally with ASA_CLIENT_ID,
ASA_TEAM_ID, ASA_KEY_ID, ASA_PRIVATE_KEY_PEM and ASA_ORG_ID set.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh_dashboard_json as legacy


def show(label, obj, depth=0, max_rows=2):
    """Print an envelope's keys, then the first rows' field names + a sample."""
    pad = "  " * (depth + 1)
    if isinstance(obj, dict):
        print(f"{pad}dict keys: {sorted(obj.keys())}")
        for k in ("data", "result", "pagination", "error"):
            if k in obj and obj[k] is not None:
                print(f"{pad}  .{k}: {type(obj[k]).__name__}")
                if isinstance(obj[k], dict):
                    print(f"{pad}    keys: {sorted(obj[k].keys())}")
                elif isinstance(obj[k], list) and obj[k]:
                    print(f"{pad}    len={len(obj[k])}, first item keys: "
                          f"{sorted(obj[k][0].keys()) if isinstance(obj[k][0], dict) else type(obj[k][0]).__name__}")
    elif isinstance(obj, list):
        print(f"{pad}list len={len(obj)}")
        for row in obj[:max_rows]:
            if isinstance(row, dict):
                print(f"{pad}  row keys: {sorted(row.keys())}")
                print(f"{pad}  sample: {json.dumps(row, default=str)[:900]}")
            else:
                print(f"{pad}  {row!r}")
    else:
        print(f"{pad}{type(obj).__name__}: {obj!r}")


def attempt(label, fn):
    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
    try:
        out = fn()
        show(label, out)
        return out
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        return None


# Endpoints safe to call ad hoc. Apple's management API creates and mutates
# campaigns through POST, so an open "call any path" tool could spend real
# money by accident. Only read-shaped endpoints are allowed: the /query and
# /find forms plus a short allowlist.
READ_ONLY_SUFFIXES = ("/query", "/find")
READ_ONLY_PATHS = ("/acls", "/me", "/campaigns", "/adgroups")


def is_read_only(method: str, path: str) -> bool:
    if method.upper() == "GET":
        return True
    p = path.split("?", 1)[0].rstrip("/")
    return p.endswith(READ_ONLY_SUFFIXES) or p in READ_ONLY_PATHS


def ad_hoc(method: str, path: str, body_json: str) -> int:
    """Call one endpoint and dump the reply. Driven by workflow inputs so the
    API can be explored without a code change and a deploy per question."""
    if not is_read_only(method, path):
        print(f"REFUSED: {method} {path} is not a read-only endpoint. "
              f"Allowed: GET anything, or POST to a path ending in "
              f"{READ_ONLY_SUFFIXES} or one of {READ_ONLY_PATHS}.")
        return 2
    try:
        body = json.loads(body_json) if body_json.strip() else {}
    except json.JSONDecodeError as e:
        print(f"body is not valid JSON: {e}")
        return 2
    print(f"{method} {path}")
    print(f"body: {json.dumps(body)[:2000]}")
    resp = attempt(f"{method} {path}", lambda: legacy.asa_v1(method, path, body))
    if resp:
        print("\n--- full response (truncated to 12k) ---")
        print(json.dumps(resp, indent=2, default=str)[:12000])
    return 0


def main() -> int:
    if not legacy.ASA_V1_ENABLED:
        print("ASA_V1_ENABLED is false — one of ASA_CLIENT_ID / ASA_TEAM_ID / "
              "ASA_KEY_ID / ASA_PRIVATE_KEY_PEM is missing.")
        return 1

    print(f"org id: {legacy.ORG_ID}")
    try:
        legacy.asa_v1_token()
        print("auth: OK")
    except Exception as e:
        print(f"auth FAILED: {type(e).__name__}: {e}")
        return 1

    # Ad-hoc mode: one endpoint, supplied by the caller.
    path = os.environ.get("PROBE_PATH", "").strip()
    if path:
        return ad_hoc(os.environ.get("PROBE_METHOD", "POST").strip() or "POST",
                      path, os.environ.get("PROBE_BODY", "") or "")

    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=30)).isoformat()
    end = today.isoformat()
    print(f"time range: {start} .. {end}")

    # ── ACL: which orgs this key can see ─────────────────────────────────
    attempt("GET /acls", lambda: legacy.asa_v1("GET", "/acls"))

    # ── Management: campaigns ────────────────────────────────────────────
    camps_raw = attempt(
        "POST /campaigns/query (raw envelope)",
        lambda: legacy.asa_v1("POST", "/campaigns/query",
                              {"pagination": {"offset": 0, "pageSize": 1000}}),
    )
    campaign_ids = []
    if isinstance(camps_raw, dict):
        rows = camps_raw.get("data") or camps_raw.get("result") or []
        if isinstance(rows, dict):
            rows = rows.get("rows") or rows.get("results") or []
        for c in rows:
            if isinstance(c, dict) and c.get("id"):
                campaign_ids.append(str(c["id"]))
        print(f"\n  -> parsed {len(campaign_ids)} campaign ids: {campaign_ids[:6]}")

    if not campaign_ids:
        print("\n!! No campaign ids parsed — later probes will be limited.")

    # ── Management: ad groups + targeting keywords ───────────────────────
    if campaign_ids:
        cid = campaign_ids[0]
        attempt(f"POST /campaigns/{cid}/adgroups/query",
                lambda: legacy.asa_v1("POST", f"/campaigns/{cid}/adgroups/query",
                                      {"pagination": {"offset": 0, "pageSize": 100}}))
        attempt(f"POST /campaigns/{cid}/adgroups/targetingkeywords/find",
                lambda: legacy.asa_v1(
                    "POST", f"/campaigns/{cid}/adgroups/targetingkeywords/find",
                    {"pagination": {"offset": 0, "pageSize": 100}}))

    # ── Reporting ────────────────────────────────────────────────────────
    def report(entity, cid=None, group_by=None, tz="UTC", empty=False):
        body = {
            "timeRange": {"start": start, "end": end, "timeZone": tz,
                          "granularity": "DAILY"},
            "sorting": [{"field": "localSpend", "order": "DESC"}],
        }
        if group_by:
            body["groupBy"] = group_by
        if cid:
            body["filters"] = [{"field": "campaignId", "operator": "EQUALS",
                                "values": [str(cid)]}]
        if empty:
            body["options"] = {"includeRows": ["EMPTY_METRICS"]}
        return legacy.asa_v1("POST", f"/reports/apps/{entity}/query", body)

    attempt("POST /reports/apps/campaigns/query",
            lambda: report("campaigns", empty=True))

    if campaign_ids:
        cid = campaign_ids[0]
        attempt(f"POST /reports/apps/keywords/query (campaign {cid})",
                lambda: report("keywords", cid=cid, group_by=["countryOrRegion"]))
        attempt(f"POST /reports/apps/keywords/query (campaign {cid}, no groupBy)",
                lambda: report("keywords", cid=cid))
        attempt(f"POST /reports/apps/adgroups/query (campaign {cid})",
                lambda: report("adgroups", cid=cid))
        attempt(f"POST /reports/apps/searchterms/query (campaign {cid})",
                lambda: report("searchterms", cid=cid, tz="ORTZ"))

    # ── Insights: search volume ──────────────────────────────────────────
    # "searchTerms" is rejected as an unrecognized property; try the plausible
    # alternatives in one pass rather than one deploy each.
    TERMS = ["instagram unfollowers", "follower tracker"]
    for body in (
        {"searchTerms": TERMS, "countriesOrRegions": ["US"]},
        {"searchTerms": TERMS, "countryOrRegion": "US"},
        {"terms": TERMS, "countriesOrRegions": ["US"]},
        {"keywords": TERMS, "countriesOrRegions": ["US"]},
        {"query": TERMS, "countriesOrRegions": ["US"]},
        {"selector": {"conditions": [
            {"field": "searchTerm", "operator": "IN", "values": TERMS}]}},
        {"filters": [{"field": "searchTerm", "operator": "IN", "values": TERMS},
                     {"field": "countryOrRegion", "operator": "EQUALS",
                      "values": ["US"]}]},
        {"countriesOrRegions": ["US"]},
        {},
    ):
        attempt(f"POST /insights/apps/search-term-popularity/query "
                f"body={sorted(body.keys())}",
                lambda b=body: legacy.asa_v1(
                    "POST", "/insights/apps/search-term-popularity/query", b))

    # ── Recommendations ──────────────────────────────────────────────────
    # "Filters are required and cannot be empty" — so supply one. Without a
    # campaign id we can still learn the accepted filter shape from the error.
    rec_filters = ([{"field": "campaignId", "operator": "IN",
                     "values": campaign_ids[:20]}] if campaign_ids else
                   [{"field": "status", "operator": "IN",
                     "values": ["ACTIVE", "APPLIED", "DISMISSED"]}])
    for path in ("/recommendations/daily-budgets/query",
                 "/recommendations/target-cpas/query",
                 "/recommendations/keywords/query"):
        attempt(f"POST {path}",
                lambda p=path: legacy.asa_v1(
                    "POST", p,
                    {"filters": rec_filters,
                     "pagination": {"offset": 0, "pageSize": 50}}))

    print(f"\n{'=' * 78}\nprobe complete\n{'=' * 78}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
