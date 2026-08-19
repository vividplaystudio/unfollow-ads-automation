#!/usr/bin/env python3
"""
Probe 2: can the RevenueCat Charts API supply every column the dashboard
already shows, segmented by ASA campaign/keyword?

Existing columns:
  CAMPAIGN COUNTRY SPEND REVENUE PROFIT ROAS PAID W M Y RENEW CANCEL
  INSTALLS CPA STATUS

SPEND / INSTALLS / STATUS come from the Apple Ads API. Everything else has to
come from RevenueCat. This finds out which charts exist, what each can be
segmented by, whether two segments can be combined (campaign + country), and
whether daily granularity is available.

Read-only.
"""
import json, os, sys, urllib.error, urllib.parse, urllib.request

KEY = os.environ["REVENUECAT_API_KEY"]
PROJECT = os.environ.get("REVENUECAT_PROJECT_ID", "6afc72a9")
BASE = "https://api.revenuecat.com/v2"


def call(path, params=None):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {KEY}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        b = e.read().decode()[:1200]
        try:
            b = json.loads(b)
        except Exception:
            pass
        return e.code, b
    except Exception as e:
        return None, str(e)


def segments_for(chart):
    """Send a deliberately invalid segment; RC replies with the valid list."""
    code, body = call(f"/projects/{PROJECT}/charts/{chart}",
                      {"segment": "__invalid__"})
    if code == 400 and isinstance(body, dict):
        msg = body.get("message", "")
        if "Supported segments" in msg:
            return msg.split("Supported segments for chart")[-1]
    return None


print("=" * 78)
print("[1] WHICH CHARTS EXIST")
print("=" * 78)
candidates = [
    "revenue", "active_subscriptions", "actives", "new_customers",
    "conversion", "conversion_to_paying", "mrr", "churn", "trials",
    "trial_conversion", "subscribers", "paid_subscribers", "initial_conversion",
    "renewals", "cancellations", "refunds", "ltv", "arr", "subscription_retention",
]
valid = []
for c in candidates:
    code, body = call(f"/projects/{PROJECT}/charts/{c}")
    if code == 200:
        valid.append(c)
        name = body.get("name") if isinstance(body, dict) else ""
        print(f"  OK   {c:<24} {name}")
    else:
        print(f"  --   {c:<24} {code}")

print()
print("=" * 78)
print("[2] SEGMENTS PER CHART  (does attribution_keyword appear?)")
print("=" * 78)
for c in valid:
    segs = segments_for(c)
    if segs:
        has_kw = "attribution_keyword" in segs
        has_camp = "attribution_campaign" in segs
        print(f"\n  {c}  [keyword={'YES' if has_kw else 'no'} campaign={'YES' if has_camp else 'no'}]")
        print(f"    {segs.strip()[:600]}")

print()
print("=" * 78)
print("[3] CAN TWO SEGMENTS COMBINE?  (CAMPAIGN + COUNTRY)")
print("=" * 78)
for params in (
    {"segment": ["attribution_campaign", "country"]},
    {"segment": "attribution_campaign,country"},
    {"segment": "attribution_campaign", "segment_2": "country"},
):
    code, body = call(f"/projects/{PROJECT}/charts/revenue", params)
    print(f"  {json.dumps(params)} -> {code}")
    if code != 200:
        print(f"    {json.dumps(body)[:300]}")

print()
print("=" * 78)
print("[4] DATE RANGE + GRANULARITY + a real keyword-segmented pull")
print("=" * 78)
for params in (
    {"segment": "attribution_keyword", "start_date": "2026-07-01", "end_date": "2026-08-01"},
    {"segment": "attribution_keyword", "start_date": "2026-07-01", "end_date": "2026-08-01", "resolution": "day"},
    {"segment": "attribution_keyword", "period": "P30D"},
):
    code, body = call(f"/projects/{PROJECT}/charts/revenue", params)
    print(f"\n  {json.dumps(params)} -> {code}")
    print(f"    {json.dumps(body)[:900]}")

print()
print("=" * 78)
print("[5] W / M / Y SPLIT — is product_duration segmentable?")
print("=" * 78)
code, body = call(f"/projects/{PROJECT}/charts/revenue",
                  {"segment": "product_duration"})
print(f"  segment=product_duration -> {code}")
print(f"    {json.dumps(body)[:700]}")
