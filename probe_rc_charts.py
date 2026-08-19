#!/usr/bin/env python3
"""
One-off probe: does the RevenueCat Charts API let us segment revenue by
Apple Search Ads keyword?

If it does, the dashboard can source ASA keyword revenue from a handful of
aggregate calls instead of walking every customer record. Prints findings to
stdout; writes nothing, changes nothing.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

KEY = os.environ.get("REVENUECAT_API_KEY", "")
PROJECT = os.environ.get("REVENUECAT_PROJECT_ID", "6afc72a9")
BASE = "https://api.revenuecat.com/v2"

if not KEY:
    print("ERROR: REVENUECAT_API_KEY not set")
    sys.exit(1)


def call(path, params=None):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {KEY}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:1500]
        try:
            body = json.loads(body)
        except Exception:
            pass
        return e.code, body
    except Exception as e:
        return None, str(e)


print("=" * 78)
print("REVENUECAT CHARTS API PROBE")
print("=" * 78)

# 1. Which chart/metrics paths exist at all?
print("\n[1] Discovering endpoints")
for path in [
    f"/projects/{PROJECT}/metrics/overview",
    f"/projects/{PROJECT}/charts",
    f"/projects/{PROJECT}/charts/options_schema",
    f"/projects/{PROJECT}/charts/schema",
    f"/projects/{PROJECT}/charts/revenue",
]:
    code, body = call(path)
    marker = "OK " if code == 200 else "-- "
    print(f"  {marker}{code}  {path}")
    if code == 200:
        preview = json.dumps(body)[:400]
        print(f"        {preview}")

# 2. The important question: supported segments for the revenue chart.
print("\n[2] Supported segments / filters")
for path in [
    f"/projects/{PROJECT}/charts/options_schema",
    f"/projects/{PROJECT}/charts/revenue/options_schema",
]:
    code, body = call(path)
    if code == 200:
        print(f"  from {path}:")
        print(json.dumps(body, indent=2)[:3000])

# 3. Ask directly for an ASA-keyword segment. A 400 is informative --
#    RevenueCat returns the list of segments it DOES support.
print("\n[3] Requesting an Apple Search Ads keyword segment")
for seg in ["apple_search_ads_keyword", "asa_keyword", "keyword"]:
    code, body = call(f"/projects/{PROJECT}/charts/revenue", {
        "start_date": "2026-07-01",
        "end_date": "2026-08-01",
        "segment": seg,
    })
    print(f"  segment={seg!r} -> HTTP {code}")
    if isinstance(body, (dict, list)):
        print("   ", json.dumps(body)[:900])
    else:
        print("   ", str(body)[:900])

print("\n" + "=" * 78)
print("READ: a 200 in [3] means ASA keyword revenue is available as an")
print("aggregate -- the per-customer walk can be deleted for ASA entirely.")
print("A 400 should list the segments that ARE supported; that tells us")
print("exactly what to build against.")
print("=" * 78)
