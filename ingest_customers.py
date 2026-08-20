"""
Cold path: reconcile the customer dimension against RevenueCat.

This is the ONE job that still has to walk the full customer list, and the
whole point of the store design is that it no longer sits on the critical
path. It runs once a night; the 15-minute hot path never touches it.

WHAT IT IS FOR
--------------
Two things the webhook log cannot tell us:

  1. Non-payers. The log only contains people who transacted. Cohort counts
     ("how many users did this channel bring") need everyone.
  2. first_seen_at / country for customers who have not paid.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not fetch per-customer attributes for the whole population. That was
the original sin of the old pipeline: 2 REST calls x every customer who ever
installed, on every run.

Instead attribution is filled in incrementally and permanently:
  * ASA payers already arrive attributed, free, in the webhook (see ingest_rc).
  * Everyone else is enriched a bounded batch at a time, newest first, and the
    result is stored forever. A customer is enriched at most once, ever.

So the backlog drains over a few nights and then only new installs cost
anything -- roughly a day's install volume, not the entire history.
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import store
import refresh_dashboard_json as legacy
from ingest_rc import normalize_channel

# Upper bound on how many customers get attribute lookups in a single run.
#
# The initial backlog is drained; this now only sees new installs.
#
# Worth recording how the yield behaves, because the first pass is misleading:
# enriching the NEWEST 40,000 customers returned zero attribution, since recent
# growth is Meta-driven and Meta attribution lives in Adjust, never reaching
# RevenueCat. The next pass over the remaining 31,515 -- the older, Apple
# Search Ads era -- returned 1,910 (6.1%). So a zero-yield batch means "these
# particular customers came from a channel RC cannot see", not "this step is
# useless".
#
# Steady state is roughly a day of installs, so a small cap is right. Raise it
# (ATTR_BATCH=40000) only to drain a fresh backlog.
ATTR_BATCH = int(os.environ.get("ATTR_BATCH", "2000"))
ATTR_WORKERS = int(os.environ.get("ATTR_WORKERS", "8"))


def sync_customer_list(conn) -> dict:
    """Walk RC's customer list and upsert identity fields.

    Only id / first_seen_at / last_seen_country come from the list endpoint --
    that is all it returns, and all we need here. Attribution is a separate,
    bounded step below.
    """
    t0 = time.time()
    customers = legacy.rc_get_all_customers()
    rows = [
        {
            "customer_id": c["id"],
            "first_seen_ms": c.get("first_seen_at"),
            "country": c.get("last_seen_country") or "",
        }
        for c in customers
        if c.get("id")
    ]
    n = store.upsert_customers(conn, rows)
    conn.commit()
    print(f"  [cold] walked {len(customers)} customers, upserted {n} "
          f"in {time.time()-t0:.0f}s")
    return {"walked": len(customers), "upserted": n}


def enrich_attribution(conn, limit: int = ATTR_BATCH) -> dict:
    """Fetch $mediaSource/$campaign/$keyword for customers we've never asked about.

    Newest first: recent installs are the ones inside the reporting windows,
    so they are worth the calls before ancient history is backfilled.

    `attrs_fetched_ms IS NULL` is the 'never asked' marker. Rows written by
    sync_customer_list above leave it NULL; ingest_rc and this function set it.
    Because it is only ever set once per customer, total lifetime cost is one
    lookup per customer rather than one per customer per run.
    """
    ids = [
        r["customer_id"]
        for r in conn.execute(
            "SELECT customer_id FROM customer "
            "WHERE attrs_fetched_ms IS NULL "
            "ORDER BY COALESCE(first_seen_ms, 0) DESC "
            "LIMIT ?",
            (int(limit),),
        )
    ]
    if not ids:
        print("  [cold] attribution backlog empty")
        return {"fetched": 0, "attributed": 0, "remaining": 0}

    t0 = time.time()
    out = []

    def one(cid):
        attrs = legacy.rc_fetch_customer_attrs(cid) or {}
        media = (attrs.get("$mediaSource") or "").strip()
        return {
            "customer_id": cid,
            "media_source": media,
            "channel": normalize_channel(media),
            "campaign": (attrs.get("$campaign") or "").strip(),
            "adgroup": (attrs.get("$adGroup") or "").strip(),
            "keyword": (attrs.get("$keyword") or "").strip().lower(),
        }

    with ThreadPoolExecutor(max_workers=ATTR_WORKERS) as exe:
        out = list(exe.map(one, ids))

    # Customers with no attribution at all still get a row written, so that
    # fetched_at_ms is stamped and we never pay to ask about them twice.
    # normalize_channel('') returns "Organic / Unattributed", which is the
    # correct answer, not a missing one.
    n = store.upsert_customers(conn, out, attribution_resolved=True)
    conn.commit()
    attributed = sum(1 for r in out if r["media_source"])
    remaining = conn.execute(
        "SELECT COUNT(*) FROM customer WHERE attrs_fetched_ms IS NULL"
    ).fetchone()[0]
    yield_pct = (attributed / n * 100) if n else 0
    print(f"  [cold] enriched {n} customers in {time.time()-t0:.0f}s: "
          f"{attributed} attributed ({yield_pct:.1f}% yield); {remaining} still unknown")
    if n >= 500 and attributed == 0:
        print("  [cold] note: zero yield on this batch -- expected while the "
              "customers being enriched came from Meta, whose attribution "
              "lives in Adjust and never reaches RevenueCat.")
    return {"fetched": n, "attributed": attributed, "remaining": remaining}


def main() -> int:
    conn = store.open_store()
    sync_customer_list(conn)
    enrich_attribution(conn)
    store.set_meta(conn, "cold_ran_at_ms", store.utc_now_ms())
    conn.commit()
    st = store.store_stats(conn)
    print(f"  [cold] store: {st['customers']} customers "
          f"({st['customers_attributed']} attributed), {st['txn']} txn")
    return 0


if __name__ == "__main__":
    sys.exit(main())
