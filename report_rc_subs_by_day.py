#!/usr/bin/env python3
"""
One-shot RevenueCat report — daily new subscriptions by product tier
from the earliest signup through today, plus cancellation counts.

Walks every customer in the project (via /v2/customers), then for each one
pulls /purchases and /subscriptions to bucket:
  * NEW SIGNUPS by (date, tier)
  * CANCELLATIONS by (date, tier)

Output is a CSV table printed to stdout so the GitHub Actions log shows it
verbatim — user can copy/paste from there.

Runtime: 15–45 min depending on customer count (rate-limited by RC API).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

API_KEY = os.environ["REVENUECAT_API_KEY"]
PROJECT_ID = os.environ.get("REVENUECAT_PROJECT_ID", "6afc72a9")

# Tier classifier — matches product_id patterns to our 3 tiers.
def classify(product_id: str) -> str:
    p = (product_id or "").lower()
    if "week" in p:
        return "weekly"
    if "month" in p:
        return "monthly"
    if "year" in p or "annual" in p:
        return "yearly"
    return "other"


def rc_get(url: str, timeout: int = 30, max_retries: int = 4) -> dict:
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"  [{e.code}] retry in {wait}s ({body[:80]})", file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body}")
        except (TimeoutError, urllib.error.URLError) as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                time.sleep(wait)
                continue
            raise RuntimeError(f"Network error: {e}")
    raise RuntimeError("Max retries exceeded")


def get_all_customers() -> list:
    """Walk all customers via /v2/projects/{id}/customers pagination."""
    all_c = []
    after = None
    page = 0
    while True:
        page += 1
        url = f"https://api.revenuecat.com/v2/projects/{PROJECT_ID}/customers?limit=1000"
        if after:
            url += f"&starting_after={urllib.parse.quote(after, safe='')}"
        data = rc_get(url)
        items = data.get("items", [])
        all_c.extend(items)
        next_page = data.get("next_page")
        if not next_page:
            break
        # RC v2 uses starting_after with the last customer's id
        after = items[-1]["id"] if items else None
        if page % 3 == 0:
            print(f"  Page {page}: {len(all_c)} customers so far", flush=True)
    return all_c


def get_customer_subs(cust_id: str) -> list:
    encoded = urllib.parse.quote(cust_id, safe="")
    url = f"https://api.revenuecat.com/v2/projects/{PROJECT_ID}/customers/{encoded}/subscriptions?limit=100"
    try:
        return rc_get(url, timeout=20, max_retries=2).get("items", [])
    except Exception as e:
        print(f"  subs err {cust_id}: {e}", file=sys.stderr)
        return []


def get_customer_purchases(cust_id: str) -> list:
    encoded = urllib.parse.quote(cust_id, safe="")
    url = f"https://api.revenuecat.com/v2/projects/{PROJECT_ID}/customers/{encoded}/purchases?limit=100"
    try:
        return rc_get(url, timeout=20, max_retries=2).get("items", [])
    except Exception as e:
        print(f"  purch err {cust_id}: {e}", file=sys.stderr)
        return []


def epoch_ms_to_date(ms) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat()


def main() -> int:
    t0 = time.time()
    print("=" * 70, flush=True)
    print(f"  RC Subs-by-day Report — Project {PROJECT_ID}", flush=True)
    print(f"  Started at {datetime.now(timezone.utc).isoformat()}", flush=True)
    print("=" * 70, flush=True)

    print("\nStep 1: Fetching all customers…", flush=True)
    customers = get_all_customers()
    print(f"  Total: {len(customers)} customers", flush=True)

    # Bucketing
    new_signups: dict = defaultdict(lambda: defaultdict(int))    # {date: {tier: count}}
    cancellations: dict = defaultdict(lambda: defaultdict(int))
    seen_products: set = set()

    print("\nStep 2: Walking purchases + subscriptions per customer…", flush=True)
    print(f"  ETA: ~{len(customers) * 0.4 / 60:.0f} min at ~0.4s/customer", flush=True)

    for i, c in enumerate(customers):
        cust_id = c.get("id")
        if not cust_id:
            continue

        # /purchases gives every payment (signup + renewals).
        # First payment for a given product = signup for that tier.
        purchases = get_customer_purchases(cust_id)
        # Sort chronologically so we can identify "first purchase" per product
        purchases.sort(key=lambda p: p.get("purchased_at_ms") or 0)
        first_purchase_by_product: dict = {}
        for p in purchases:
            product = p.get("product_id") or p.get("store_product_id") or ""
            purchased_at_ms = p.get("purchased_at_ms") or p.get("date_ms")
            if not product or not purchased_at_ms:
                continue
            seen_products.add(product)
            if product not in first_purchase_by_product:
                first_purchase_by_product[product] = purchased_at_ms

        # For each product this customer bought, count the first purchase as a signup
        for product, first_ms in first_purchase_by_product.items():
            date = epoch_ms_to_date(first_ms)
            tier = classify(product)
            if date:
                new_signups[date][tier] += 1

        # /subscriptions gives cancellation status. RC exposes `unsubscribe_detected_at_ms`
        # or expires_date_ms for cancellations. A subscription is a "cancellation event"
        # if unsubscribe_detected_at exists, or if status is CANCELLED.
        subs = get_customer_subs(cust_id)
        for s in subs:
            product = s.get("product_id") or s.get("store_product_id") or ""
            unsub_ms = (
                s.get("unsubscribe_detected_at_ms")
                or s.get("cancelled_at_ms")
                or (s.get("expires_date_ms") if s.get("status") == "CANCELLED" else None)
            )
            if unsub_ms and product:
                date = epoch_ms_to_date(unsub_ms)
                tier = classify(product)
                if date:
                    cancellations[date][tier] += 1

        if (i + 1) % 250 == 0:
            elapsed = time.time() - t0
            eta_remaining = elapsed * (len(customers) / (i + 1) - 1)
            print(
                f"  {i+1}/{len(customers)} — elapsed {elapsed:.0f}s, ETA {eta_remaining:.0f}s",
                flush=True,
            )

    elapsed = time.time() - t0
    print(f"\nDone walking in {elapsed:.0f}s", flush=True)
    print(f"Unique products seen: {sorted(seen_products)}", flush=True)

    # === OUTPUT: CSV table ===
    all_dates = sorted(set(new_signups.keys()) | set(cancellations.keys()))
    print("\n\n" + "=" * 70)
    print("  CSV — DAILY NEW SIGNUPS + CANCELLATIONS BY TIER")
    print("=" * 70)
    print(
        "DATE,new_weekly,new_monthly,new_yearly,new_other,new_total,"
        "cancel_weekly,cancel_monthly,cancel_yearly,cancel_other,cancel_total,net_delta"
    )
    tot_nw = tot_nm = tot_ny = tot_no = 0
    tot_cw = tot_cm = tot_cy = tot_co = 0
    for date in all_dates:
        n = new_signups.get(date, {})
        c = cancellations.get(date, {})
        nw = n.get("weekly", 0); nm = n.get("monthly", 0); ny = n.get("yearly", 0); no = n.get("other", 0)
        cw = c.get("weekly", 0); cm = c.get("monthly", 0); cy = c.get("yearly", 0); co = c.get("other", 0)
        ntot = nw + nm + ny + no
        ctot = cw + cm + cy + co
        net = ntot - ctot
        tot_nw += nw; tot_nm += nm; tot_ny += ny; tot_no += no
        tot_cw += cw; tot_cm += cm; tot_cy += cy; tot_co += co
        print(
            f"{date},{nw},{nm},{ny},{no},{ntot},{cw},{cm},{cy},{co},{ctot},{net:+d}"
        )

    print(
        f"TOTAL,,{tot_nw},{tot_nm},{tot_ny},{tot_no},"
        f"{tot_nw + tot_nm + tot_ny + tot_no},"
        f"{tot_cw},{tot_cm},{tot_cy},{tot_co},"
        f"{tot_cw + tot_cm + tot_cy + tot_co}"
    )

    print("\n\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    tot_new = tot_nw + tot_nm + tot_ny + tot_no
    tot_cancel = tot_cw + tot_cm + tot_cy + tot_co
    print(f"\nLifetime NEW SIGNUPS:")
    print(f"  Weekly:  {tot_nw:>6,}  ({tot_nw/tot_new*100 if tot_new else 0:.1f}%)")
    print(f"  Monthly: {tot_nm:>6,}  ({tot_nm/tot_new*100 if tot_new else 0:.1f}%)")
    print(f"  Yearly:  {tot_ny:>6,}  ({tot_ny/tot_new*100 if tot_new else 0:.1f}%)")
    print(f"  Other:   {tot_no:>6,}  ({tot_no/tot_new*100 if tot_new else 0:.1f}%)")
    print(f"  TOTAL:   {tot_new:>6,}")
    print(f"\nLifetime CANCELLATIONS:")
    print(f"  Weekly:  {tot_cw:>6,}")
    print(f"  Monthly: {tot_cm:>6,}")
    print(f"  Yearly:  {tot_cy:>6,}")
    print(f"  Other:   {tot_co:>6,}")
    print(f"  TOTAL:   {tot_cancel:>6,}")
    print(f"\nNet active adds (signups - cancels): {tot_new - tot_cancel:,}")
    print(f"\nDate range: {all_dates[0] if all_dates else '—'} → {all_dates[-1] if all_dates else '—'}")
    print(f"Days with activity: {len(all_dates)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
