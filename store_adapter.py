"""
Feed the existing dashboard builder from the fact store.

WHY AN ADAPTER RATHER THAN A REWRITE
------------------------------------
refresh_dashboard_json.py contains months of accumulated, working aggregation
logic: the reporting windows, the cohort rules, the channel normalization, the
campaign/keyword/adgroup rollups and every column the dashboard renders. None
of that is wrong -- the only broken part is HOW it gets its input, which was
"poll RevenueCat for all 111,351 customers, twice each, every 30 minutes."

So this module reproduces exactly the customer-dict shape that
rc_get_all_customers() + enrich_one() used to produce, but builds it with one
SQL query against the store. The builder downstream cannot tell the difference,
which means the risky part of the migration -- re-deriving business logic -- is
avoided entirely.

The shape the builder expects, per customer:
    id, first_seen_at (ms), last_seen_country,
    _attrs           {"$mediaSource","$campaign","$keyword","$adGroup"}
    _revenue         float, lifetime
    _active          bool
    _canceled        bool
    _renewals        int
    _tier            str
    _tier_counts     {"weekly": n, "monthly": n, "yearly": n}
    _tier_revenue    {"weekly": $, "monthly": $, "yearly": $}
    _sub_count       int
    _transactions    [{ts, amount, tier, is_renewal}, ...]
    _txn_source      str

REVENUE SEMANTICS
-----------------
`amount` on each transaction stays GROSS, because every existing ROAS and
revenue column in the dashboard is gross and changing that silently would
reprice every historical number on the page. The true post-VAT, post-Apple
figure travels alongside as `proceeds`, and `_proceeds` per customer, so
profit columns can move to it deliberately rather than by accident.
"""

from collections import defaultdict

import store


def _channel_to_attrs(row) -> dict:
    """Rebuild the $-prefixed attribute dict the builder reads."""
    attrs = {}
    if row["media_source"]:
        attrs["$mediaSource"] = row["media_source"]
    if row["campaign"]:
        attrs["$campaign"] = row["campaign"]
    if row["keyword"]:
        attrs["$keyword"] = row["keyword"]
    if row["adgroup"]:
        attrs["$adGroup"] = row["adgroup"]
    return attrs


def customers_from_store(conn, include_non_payers: bool = True) -> list:
    """Materialize the legacy customer list from the store.

    One pass over txn (grouped in Python rather than SQL because the builder
    wants the raw transaction list anyway), one pass over customer, one over
    sub_state. No network calls at all.
    """
    # ── transactions, grouped by customer ────────────────────────────────
    txns_by_cust = defaultdict(list)
    first_txn_ms = {}
    txn_country = {}
    rev = defaultdict(float)
    proceeds = defaultdict(float)
    renewals = defaultdict(int)
    tier_counts = defaultdict(lambda: defaultdict(int))
    tier_revenue = defaultdict(lambda: defaultdict(float))

    for t in conn.execute(
        "SELECT customer_id, ts_ms, amount, proceeds, tier, is_renewal, country "
        "FROM txn WHERE amount > 0 ORDER BY ts_ms"
    ):
        cid = t["customer_id"]
        txns_by_cust[cid].append({
            "ts": t["ts_ms"],
            "amount": float(t["amount"]),
            "proceeds": float(t["proceeds"]),
            "tier": t["tier"],
            "is_renewal": bool(t["is_renewal"]),
        })
        # Cheapest safe stand-in for an unknown install date. build_revenue_index
        # SKIPS any customer whose first_seen_at is falsy, so without this a
        # payer who bought since the last nightly customer walk would drop out
        # of the channel and keyword tables entirely -- revenue silently
        # missing rather than visibly late. Their first transaction is an upper
        # bound on install date and keeps them in the right reporting window.
        if cid not in first_txn_ms:
            first_txn_ms[cid] = t["ts_ms"]
            if t["country"]:
                txn_country[cid] = t["country"]
        rev[cid] += float(t["amount"])
        proceeds[cid] += float(t["proceeds"])
        if t["is_renewal"]:
            renewals[cid] += 1
        tier = t["tier"] or "other"
        tier_counts[cid][tier] += 1
        tier_revenue[cid][tier] += float(t["amount"])

    # ── subscription state ───────────────────────────────────────────────
    state = {
        r["customer_id"]: r
        for r in conn.execute(
            "SELECT customer_id, active, canceled, weekly, monthly, yearly, sub_count "
            "FROM sub_state"
        )
    }

    # ── attribution / identity ───────────────────────────────────────────
    attr_rows = {
        r["customer_id"]: r
        for r in conn.execute(
            "SELECT customer_id, first_seen_ms, country, media_source, channel, "
            "       campaign, adgroup, keyword FROM customer"
        )
    }

    ids = set(txns_by_cust) | set(state)
    if include_non_payers:
        ids |= set(attr_rows)

    out = []
    for cid in ids:
        a = attr_rows.get(cid)
        s = state.get(cid)
        tc = tier_counts.get(cid, {})
        # Dominant tier by transaction count, matching the builder's meaning
        # of "_tier" (which product this customer mainly is), not most recent.
        dominant = max(tc, key=tc.get) if tc else "none"
        out.append({
            "id": cid,
            "first_seen_at": (a["first_seen_ms"] if a and a["first_seen_ms"]
                              else first_txn_ms.get(cid)),
            "last_seen_country": ((a["country"] if a and a["country"] else None)
                                  or txn_country.get(cid) or ""),
            "_attrs": _channel_to_attrs(a) if a else {},
            "_revenue": round(rev.get(cid, 0.0), 4),
            "_proceeds": round(proceeds.get(cid, 0.0), 4),
            "_active": bool(s["active"]) if s else False,
            "_canceled": bool(s["canceled"]) if s else False,
            "_renewals": renewals.get(cid, 0),
            "_tier": dominant,
            "_tier_counts": {
                "weekly": tc.get("weekly", 0),
                "monthly": tc.get("monthly", 0),
                "yearly": tc.get("yearly", 0),
            },
            "_tier_revenue": dict(tier_revenue.get(cid, {})),
            "_sub_count": (s["sub_count"] if s else 0),
            "_transactions": txns_by_cust.get(cid, []),
            "_txn_source": "store",
        })
    return out


def adapter_stats(customers) -> dict:
    paying = [c for c in customers if c["_revenue"] > 0]
    return {
        "customers": len(customers),
        "paying": len(paying),
        "active": sum(1 for c in customers if c["_active"]),
        "gross": round(sum(c["_revenue"] for c in customers), 2),
        "proceeds": round(sum(c["_proceeds"] for c in customers), 2),
        "attributed": sum(1 for c in customers if c["_attrs"]),
    }


if __name__ == "__main__":
    conn = store.open_store()
    cs = customers_from_store(conn)
    st = adapter_stats(cs)
    for k, v in st.items():
        print(f"  {k:12} {v}")
    if st["gross"]:
        print(f"  {'take-home':12} {st['proceeds']/st['gross']*100:.1f}%")
