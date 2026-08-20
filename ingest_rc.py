"""
Ingest RevenueCat webhook events into the fact store.

This replaces the customer-walk half of the old pipeline. The webhook receiver
already writes every event to rc_events.jsonl (rotating to .archive at 10 MB),
so the complete transaction history is on disk. Reading it is local file I/O:
8,014 events parse and land in the store in ~0.1s, versus 70 minutes to
re-derive the same numbers by polling the API customer by customer.

THREE THINGS THE EVENT LOG GIVES US FOR FREE
--------------------------------------------
1. Exact money. Every event carries `price` and `tax_percentage`, so proceeds
   are computed per transaction instead of by a flat multiplier. See
   PROCEEDS below -- this matters a lot and the old flat 0.85 was wrong.

2. ASA attribution. `subscriber_attributes` carries $mediaSource / $campaign /
   $adGroup / $keyword for Apple Search Ads installs, so keyword-level revenue
   needs no per-customer API lookup at all.

3. Subscription state. `expiration_at_ms` plus the CANCELLATION / EXPIRATION /
   UNCANCELLATION event types are enough to derive who is currently active and
   who has turned off auto-renew, again with no API call.

PROCEEDS
--------
    proceeds = price x (1 - tax_percentage) x 0.85

Apple deducts VAT first, then takes its cut. The account is enrolled in the
Small Business Program, so that cut is 15%.

Do NOT use RevenueCat's `commission_percentage`: it reports the standard 30%
and does not model the Small Business Program (every US event in the log reads
exactly 0.30, and `takehome_percentage` never once reads 0.85). It is stored
anyway, unused, so the decision is auditable later.

The old flat `x 0.85` ignored VAT completely. That is right for the US, Canada
and Israel at 0% VAT, and wrong by 22-27 points across Europe -- which is where
break-even ROAS is really 140-145%, not 118%.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

import store

# Small Business Program: Apple takes 15% of the post-VAT amount.
STORE_TAKE = 0.85

# Events that move money.
REVENUE_TYPES = {"INITIAL_PURCHASE", "RENEWAL", "NON_RENEWING_PURCHASE"}
# PRODUCT_CHANGE carries price 0 in this account's log; kept separate so a
# future plan change with real value is not silently dropped.
REVENUE_TYPES_OPTIONAL = {"PRODUCT_CHANGE"}

TIER_BY_PRODUCT = {
    "com_weekly": "weekly",
    "com_monthly": "monthly",
    "com_yearly": "yearly",
}

WEBHOOK_URL = os.environ.get(
    "RC_EVENTS_URL", "https://genivox.com/ads-upload/rc_events.php"
)
RC_WEBHOOK_SECRET = os.environ.get("RC_WEBHOOK_SECRET", "")
LOCAL_OUTPUT_DIR = os.environ.get("LOCAL_OUTPUT_DIR", "")


def _tier(product_id: str, expiration_ms, purchased_ms) -> str:
    """Tier from product id, falling back to the billing period length.

    The product-id map covers this app's own SKUs; the duration fallback keeps
    renamed or newly added SKUs from all landing in 'other'.
    """
    t = TIER_BY_PRODUCT.get((product_id or "").strip())
    if t:
        return t
    try:
        days = (int(expiration_ms) - int(purchased_ms)) / 86400000.0
    except (TypeError, ValueError):
        return "other"
    if days <= 0:
        return "other"
    if days < 12:
        return "weekly"
    if days < 200:
        return "monthly"
    return "yearly"


def parse_event(rec: dict):
    """One log line -> (txn row or None, customer attribution row or None)."""
    e = rec.get("event") or rec
    event_id = e.get("id")
    customer_id = e.get("app_user_id") or e.get("original_app_user_id")
    if not event_id or not customer_id:
        return None, None

    etype = e.get("type") or ""
    is_revenue = etype in REVENUE_TYPES or (
        etype in REVENUE_TYPES_OPTIONAL and float(e.get("price") or 0) > 0
    )

    # Bucket money by WHEN IT WAS PAID, and state changes by when they
    # happened. For CANCELLATION/EXPIRATION, purchased_at_ms still points at
    # the ORIGINAL purchase -- using it there would file a cancellation under
    # the month the subscription started and skew every daily series.
    if is_revenue:
        ts = e.get("purchased_at_ms") or e.get("event_timestamp_ms")
    else:
        ts = e.get("event_timestamp_ms") or e.get("purchased_at_ms")
    if not ts:
        return None, None

    price = float(e.get("price") or 0)
    tax = float(e.get("tax_percentage") or 0)
    # Guard against a malformed tax figure silently inflating or zeroing revenue.
    if not (0.0 <= tax < 1.0):
        tax = 0.0

    txn = {
        "event_id": event_id,
        "customer_id": customer_id,
        "ts_ms": int(ts),
        "amount": price if is_revenue else 0.0,
        "proceeds": round(price * (1 - tax) * STORE_TAKE, 4) if is_revenue else 0.0,
        "tax_pct": tax,
        "commission_pct": e.get("commission_percentage"),
        "expiration_ms": e.get("expiration_at_ms"),
        "tier": _tier(e.get("product_id"), e.get("expiration_at_ms"),
                      e.get("purchased_at_ms")),
        "is_renewal": 1 if etype == "RENEWAL" else 0,
        # A RevenueCat REFUND arrives as CANCELLATION with a refund reason;
        # a user simply switching off auto-renew is also CANCELLATION but
        # keeps its value. Only the former claws money back.
        "is_refund": 1 if (
            etype == "CANCELLATION"
            and (e.get("cancel_reason") or "") in ("CUSTOMER_SUPPORT", "REFUNDED")
        ) else 0,
        "product_id": e.get("product_id"),
        "store": e.get("store"),
        "country": e.get("country_code"),
        "event_type": etype,
    }

    cust = None
    sa = e.get("subscriber_attributes") or {}

    def attr(name):
        v = sa.get(name)
        if isinstance(v, dict):
            return (v.get("value") or "").strip()
        return (v or "").strip() if isinstance(v, str) else ""

    media = attr("$mediaSource")
    campaign = attr("$campaign")
    keyword = attr("$keyword")
    adgroup = attr("$adGroup")
    if media or campaign or keyword:
        cust = {
            "customer_id": customer_id,
            "country": e.get("country_code"),
            "media_source": media,
            "channel": normalize_channel(media),
            "campaign": campaign,
            "adgroup": adgroup,
            "keyword": keyword.lower(),
            "attrs_json": json.dumps(
                {k: attr(k) for k in ("$mediaSource", "$campaign", "$adGroup", "$keyword")},
                separators=(",", ":"),
            ),
        }
    return txn, cust


def normalize_channel(media_source: str) -> str:
    """Same buckets the dashboard has always grouped by."""
    ms = (media_source or "").strip()
    if not ms:
        return "Organic / Unattributed"
    low = ms.lower()
    if ms == "Apple Search Ads" or "apple" in low:
        return "Apple Search Ads"
    if "facebook" in low or "meta" in low:
        return "Facebook / Meta"
    if "google" in low:
        return "Google Ads"
    if "tiktok" in low:
        return "TikTok"
    return ms


def iter_log_lines(since_ms: int = 0):
    """Yield raw JSON objects from the webhook log, oldest first.

    Prefers reading from disk (running on the cPanel host). Falls back to the
    PHP endpoint over HTTP so the same script works from a laptop or CI.
    """
    if LOCAL_OUTPUT_DIR:
        import glob
        paths = sorted(glob.glob(os.path.join(LOCAL_OUTPUT_DIR, "rc_events-*.jsonl.archive")))
        live = os.path.join(LOCAL_OUTPUT_DIR, "rc_events.jsonl")
        if os.path.exists(live):
            paths.append(live)          # live file last: archives are older
        if paths:
            for path in paths:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            try:
                                yield json.loads(line)
                            except json.JSONDecodeError:
                                continue
            return

    url = f"{WEBHOOK_URL}?since_ms={int(since_ms)}&limit=200000"
    req = urllib.request.Request(url)
    if RC_WEBHOOK_SECRET:
        req.add_header("Authorization", f"Bearer {RC_WEBHOOK_SECRET}")
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.loads(resp.read().decode())
    for rec in payload.get("events", payload if isinstance(payload, list) else []):
        yield rec


def rebuild_sub_state(conn) -> int:
    """Derive active / cancelled / tier counts purely from the event stream.

    Active  = the newest expiration this customer ever bought is in the future.
    Cancelled = their most recent state event was a CANCELLATION with no later
                UNCANCELLATION (auto-renew off; note they may still be active
                until the current period runs out).

    Doing this in SQL over the store replaces two REST calls per customer.
    """
    now_ms = store.utc_now_ms()
    conn.execute("DELETE FROM sub_state")
    conn.execute(
        """
        INSERT INTO sub_state (customer_id, active, canceled, weekly, monthly,
                               yearly, sub_count, updated_at_ms)
        SELECT
            t.customer_id,
            CASE WHEN MAX(COALESCE(t.expiration_ms, 0)) > ? THEN 1 ELSE 0 END,
            CASE WHEN (
                SELECT s.event_type FROM txn s
                WHERE s.customer_id = t.customer_id
                  AND s.event_type IN ('CANCELLATION', 'UNCANCELLATION')
                ORDER BY s.ts_ms DESC LIMIT 1
            ) = 'CANCELLATION' THEN 1 ELSE 0 END,
            SUM(CASE WHEN t.tier='weekly'  AND t.amount > 0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN t.tier='monthly' AND t.amount > 0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN t.tier='yearly'  AND t.amount > 0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN t.amount > 0 THEN 1 ELSE 0 END),
            ?
        FROM txn t
        GROUP BY t.customer_id
        """,
        (now_ms, now_ms),
    )
    return conn.execute("SELECT COUNT(*) FROM sub_state").fetchone()[0]


def main() -> int:
    conn = store.open_store()
    watermark = store.get_meta_int(conn, "rc_watermark_ms", 0)

    txns, custs = [], []
    seen = 0
    bad = 0
    newest = watermark
    for rec in iter_log_lines(since_ms=max(0, watermark - 86400000)):  # 1d overlap
        seen += 1
        try:
            t, c = parse_event(rec)
        except Exception:
            bad += 1
            continue
        if t:
            txns.append(t)
            newest = max(newest, t["ts_ms"])
        if c:
            custs.append(c)

    n_txn = store.upsert_txns(conn, txns)
    # These came with real attribution attached to the purchase event, so
    # they never need an API lookup -- mark them resolved.
    n_cust = store.upsert_customers(conn, custs, attribution_resolved=True)
    n_state = rebuild_sub_state(conn)
    store.set_meta(conn, "rc_watermark_ms", newest)
    store.set_meta(conn, "rc_ingested_at", datetime.now(timezone.utc).isoformat())
    conn.commit()

    st = store.store_stats(conn)
    print(f"  [rc] read {seen} log records ({bad} unparseable)")
    print(f"  [rc] wrote {n_txn} txn, {n_cust} attributed customers, {n_state} sub_state")
    print(f"  [rc] store now: {st['txn']} txn "
          f"{st['txn_first_day']}..{st['txn_last_day']} | "
          f"gross ${st['txn_gross']:,.2f} -> proceeds ${st['txn_proceeds']:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
