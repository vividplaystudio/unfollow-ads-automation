"""
Ingest ad-side spend into the fact store.

WHY A TRAILING WINDOW RATHER THAN "EVERYTHING"
----------------------------------------------
Ad spend is immutable after a short restatement period. Apple and Meta may
revise the last couple of days as attribution settles; anything older is
final and will never change again.

So there is no reason to re-download months of history on every run. This
fetches a trailing window (default 7 days, comfortably wider than the ~3-day
restatement) and upserts it. Days outside the window keep whatever was last
written -- which is the point of a store: settled facts stay settled.

The composite primary key on ad_daily is what makes the overlap safe: a
re-fetched day REPLACES its row rather than adding to it, so running this
every 15 minutes cannot double-count.

GRANULARITY
-----------
Apple reports at keyword level with DAILY granularity, which is exactly the
grain the dashboard wants, so keyword rows go in as-is and campaign/adgroup
totals are derived by summing them rather than stored twice. Meta has no
keyword concept, so its rows carry keyword_id=''.
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import store
import refresh_dashboard_json as legacy

# Wider than the ~3-day attribution restatement window, narrow enough that a
# refresh stays cheap. Override for a backfill.
LOOKBACK_DAYS = int(os.environ.get("AD_LOOKBACK_DAYS", "7"))


def _day_list(days: int):
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def _metric(d: dict, *names):
    """Apple nests metrics under 'total'; names vary by entity."""
    tot = d.get("total") or d
    for n in names:
        if n in tot and tot[n] is not None:
            v = tot[n]
            if isinstance(v, dict):          # localSpend -> {"amount": "1.23"}
                v = v.get("amount", 0)
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def ingest_apple(conn, days: int = LOOKBACK_DAYS) -> dict:
    """Apple Ads keyword-level daily spend -> ad_daily."""
    if not legacy.ASA_V1_ENABLED:
        print("  [ads] Apple Ads v1 not configured -- skipped")
        return {"rows": 0}

    try:
        legacy.asa_v1_token()
    except Exception as e:
        print(f"  [ads] Apple auth failed ({type(e).__name__}) -- skipped")
        return {"rows": 0}

    window = _day_list(days)
    start, end = window[0], window[-1]
    t0 = time.time()

    # Keyword reports are per-campaign in v1 (a campaignId filter is required),
    # so the campaign list has to exist before this runs -- ingest_apple_dims()
    # populates it, and main() calls that first.
    campaign_ids = [
        r["campaign_id"] for r in conn.execute(
            "SELECT campaign_id FROM campaign_dim WHERE source='apple'"
        )
    ]
    if not campaign_ids:
        print("  [ads] no Apple campaigns known -- run ingest_apple_dims first")
        return {"rows": 0}

    rows, kw_dim = [], []
    report = legacy.asa_v1_report_all_campaigns(
        "keywords", start, end, campaign_ids, group_by=["countryOrRegion"]
    )

    for r in report:
        meta = r.get("metadata") or {}
        kid = str(meta.get("keywordId") or "")
        if not kid:
            continue
        kw_dim.append({
            "keyword_id": kid,
            "campaign_id": str(meta.get("campaignId") or ""),
            "adgroup_id": str(meta.get("adGroupId") or ""),
            "text": meta.get("keyword") or meta.get("keywordDisplayText"),
            "match_type": meta.get("matchType"),
            "bid": _metric(meta.get("bidAmount") or {}, "amount") or None,
            "status": meta.get("keywordStatus") or meta.get("keywordDisplayStatus"),
        })
        # Apple returns one 'granularity' entry per day inside each row.
        for g in (r.get("granularity") or []):
            day = (g.get("date") or "")[:10]
            if not day:
                continue
            rows.append({
                "day": day,
                "source": "apple",
                "campaign_id": str(meta.get("campaignId") or ""),
                "adgroup_id": str(meta.get("adGroupId") or ""),
                "keyword_id": kid,
                "country": meta.get("countryOrRegion") or "",
                "spend": _metric(g, "localSpend"),
                "impressions": int(_metric(g, "impressions")),
                "taps": int(_metric(g, "taps")),
                "installs": int(_metric(g, "totalInstalls", "installs", "conversions")),
            })

    n = store.upsert_ad_daily(conn, rows)
    store.upsert_keyword_dim(conn, kw_dim)
    conn.commit()
    print(f"  [ads] apple: {n} keyword-days {start}..{end} "
          f"({len(kw_dim)} keywords across {len(campaign_ids)} campaigns) "
          f"in {time.time()-t0:.0f}s")
    return {"rows": n}


def ingest_apple_popularity(conn) -> dict:
    """Attach Apple's search-volume score to every keyword we know about.

    Popularity is a slow-moving property of the SEARCH TERM, not of a day, so
    it belongs on the dimension and only needs refreshing occasionally --
    nightly is plenty. NULL is preserved as 'unknown'; it must never become 0,
    which would read as 'nobody searches this'.
    """
    if not legacy.ASA_V1_ENABLED:
        return {"resolved": 0}
    rows = list(conn.execute(
        "SELECT keyword_id, text, campaign_id FROM keyword_dim "
        "WHERE text IS NOT NULL AND text <> ''"
    ))
    if not rows:
        return {"resolved": 0}

    # Popularity is country-specific; group by the campaign's country so a US
    # term is not scored against a UK audience.
    countries = {
        r["campaign_id"]: (r["country"] or "US")
        for r in conn.execute("SELECT campaign_id, country FROM campaign_dim")
    }
    # One request per term per country, so skip terms already scored this month
    # rather than re-asking every night for a number that changes monthly.
    already = {
        r["keyword_id"] for r in conn.execute(
            "SELECT keyword_id FROM keyword_dim "
            "WHERE popularity IS NOT NULL AND popularity_month = ?",
            (datetime.now(timezone.utc).strftime("%Y-%m"),))
    }
    rows = [r for r in rows if r["keyword_id"] not in already]
    if not rows:
        print("  [ads] popularity already current for every keyword this month")
        return {"resolved": 0}

    by_country = {}
    for r in rows:
        c = countries.get(r["campaign_id"], "US") or "US"
        by_country.setdefault(c, []).append(r)

    updates, resolved = [], 0
    for country, group in by_country.items():
        terms = sorted({(r["text"] or "").strip() for r in group if r["text"]})
        try:
            pop = legacy.asa_v1_keyword_popularity(terms, country=country)
        except Exception as e:
            print(f"  [ads] popularity lookup failed for {country}: {type(e).__name__}")
            continue
        for r in group:
            v = pop.get((r["text"] or "").strip().lower())
            if not v:
                continue
            updates.append({
                "keyword_id": r["keyword_id"],
                "popularity": v["popularity"],
                "rank_in_genre": v.get("rank_in_genre"),
                "genre": v.get("genre"),
                "popularity_month": v.get("month"),
            })
            resolved += 1

    if updates:
        store.upsert_keyword_dim(conn, updates)
        conn.commit()
    print(f"  [ads] popularity resolved for {resolved}/{len(rows)} keywords")
    return {"resolved": resolved}


def _amount(obj):
    """Apple wraps money as {"amount": "5", "currency": "USD"}."""
    if isinstance(obj, dict):
        obj = obj.get("amount")
    try:
        return float(obj)
    except (TypeError, ValueError):
        return None


def _campaign_country(c: dict):
    """Campaigns carry their market inside `targeting`, not at the top level."""
    for holder in (c, c.get("targeting") or {}):
        v = holder.get("countriesOrRegions")
        if isinstance(v, list) and v:
            return v[0]
        if isinstance(v, str) and v:
            return v
    return None


def ingest_apple_dims(conn) -> dict:
    """Campaigns, ad groups and keywords -- the structure, not the metrics.

    This has to exist independently of spend. Reports only return rows for
    targeting that actually spent, so with every campaign paused the reporting
    endpoints correctly return nothing and the dashboard would show no
    keywords at all. The management endpoints list them regardless, which is
    also what lets popularity be attached to a keyword before it has ever run.

    Endpoint shapes, established by probing the live API:
      /campaigns/query   no filter needed; result is a bare list
      /adgroups/query    requires a campaignId filter
      /keywords/query    requires an adGroupId OR campaignId condition
    """
    if not legacy.ASA_V1_ENABLED:
        return {"campaigns": 0, "adgroups": 0, "keywords": 0}

    try:
        camps = legacy.asa_v1_paged("/campaigns/query", {})
    except Exception as e:
        print(f"  [ads] campaign fetch failed: {type(e).__name__}: {e}")
        return {"campaigns": 0, "adgroups": 0, "keywords": 0}

    camp_rows, camp_ids = [], []
    for c in camps:
        cid = str(c.get("id") or "")
        if not cid:
            continue
        camp_ids.append(cid)
        camp_rows.append({
            "campaign_id": cid,
            "source": "apple",
            "name": c.get("name"),
            "country": _campaign_country(c),
            "status": c.get("status") or c.get("displayStatus"),
            "daily_budget": _amount(c.get("dailyBudget")),
        })
    store.upsert_campaign_dim(conn, camp_rows)

    ag_rows, kw_rows = [], []
    for cid in camp_ids:
        f = [{"field": "campaignId", "operator": "EQUALS", "value": cid}]
        try:
            for g in legacy.asa_v1_paged("/adgroups/query", {"filters": f}):
                if not g.get("id"):
                    continue
                ag_rows.append({
                    "adgroup_id": str(g["id"]),
                    "campaign_id": str(g.get("campaignId") or cid),
                    "source": "apple",
                    "name": g.get("name"),
                    "status": g.get("status") or g.get("displayStatus"),
                    "daily_budget": _amount(
                        (g.get("bidStrategy") or {}).get("bid")),
                })
        except Exception as e:
            print(f"  [ads] adgroups failed for campaign {cid}: {type(e).__name__}")
        try:
            for k in legacy.asa_v1_paged("/keywords/query", {"filters": f}):
                if not k.get("id"):
                    continue
                kw_rows.append({
                    "keyword_id": str(k["id"]),
                    "campaign_id": str(k.get("campaignId") or cid),
                    "adgroup_id": str(k.get("adGroupId") or ""),
                    "text": k.get("text"),
                    "match_type": k.get("matchType"),
                    "bid": _amount(k.get("bid")),
                    "status": k.get("status") or k.get("displayStatus"),
                })
        except Exception as e:
            print(f"  [ads] keywords failed for campaign {cid}: {type(e).__name__}")

    store.upsert_adgroup_dim(conn, ag_rows)
    store.upsert_keyword_dim(conn, kw_rows)
    conn.commit()
    print(f"  [ads] apple dims: {len(camp_rows)} campaigns, {len(ag_rows)} ad groups, "
          f"{len(kw_rows)} keywords")
    return {"campaigns": len(camp_rows), "adgroups": len(ag_rows),
            "keywords": len(kw_rows)}


def main() -> int:
    conn = store.open_store()
    ingest_apple_dims(conn)
    ingest_apple(conn)
    if os.environ.get("REFRESH_POPULARITY", "1") == "1":
        ingest_apple_popularity(conn)
    store.set_meta(conn, "ads_ingested_at_ms", store.utc_now_ms())
    conn.commit()
    st = store.store_stats(conn)
    print(f"  [ads] store: {st['ad_daily']} ad-days (latest {st['ad_daily_last']}), "
          f"{st['keywords']} keywords, {st['keywords_with_popularity']} with popularity")
    return 0


if __name__ == "__main__":
    sys.exit(main())
