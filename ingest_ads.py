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
    """Unwrap Apple's money shapes.

    Keyword bids arrive as {"amount": "4", "currency": "USD"} but campaign
    budgets add a level: {"value": {"amount": "100", "currency": "USD"}}.
    Reading only the flat form stored every budget as None.
    """
    seen = 0
    while isinstance(obj, dict) and seen < 4:
        obj = obj.get("amount", obj.get("value"))
        seen += 1
    try:
        return float(obj)
    except (TypeError, ValueError):
        return None


def _campaign_country(c: dict):
    """The campaign's market, from targeting.countryOrRegion.include.

    Note the SINGULAR "countryOrRegion", and that it is an include/exclude
    object rather than a list. Looking for a plural "countriesOrRegions"
    matched nothing, so every campaign silently fell back to US -- and since
    popularity is scored per country, UK and CA keywords were being looked up
    against the US index and returning no rows. That is the difference between
    29 and most of 1,152 keywords getting a volume score.
    """
    t = c.get("targeting") or {}
    cor = t.get("countryOrRegion") or t.get("countriesOrRegions") or {}
    if isinstance(cor, dict):
        inc = cor.get("include")
        if isinstance(inc, list) and inc:
            return inc[0]
        if isinstance(inc, str) and inc:
            return inc
    elif isinstance(cor, list) and cor:
        return cor[0]
    elif isinstance(cor, str) and cor:
        return cor
    # Fall back to the top level in case a campaign type reports it there.
    v = c.get("countriesOrRegions") or c.get("countryOrRegion")
    if isinstance(v, list) and v:
        return v[0]
    return v if isinstance(v, str) and v else None


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


# Markets worth pulling the volume feed for. The keyword-research value is in
# the markets we advertise in or plan to, not every storefront Apple has.
DISCOVERY_MARKETS = [
    m.strip().upper()
    for m in os.environ.get(
        "DISCOVERY_MARKETS", "US,GB,CA,AU,DE,FR,IT,ES,BR,MX"
    ).split(",") if m.strip()
]
# Genres an unfollower/analytics app competes in.
DISCOVERY_GENRES = [
    g.strip().upper()
    for g in os.environ.get(
        "DISCOVERY_GENRES", "SOCIAL_NETWORKING,PHOTO_AND_VIDEO,UTILITIES"
    ).split(",") if g.strip()
]


def ingest_top_search_terms(conn, markets=None, genres=None,
                            page_size: int = 1000) -> dict:
    """Apple's most-searched terms per market and genre.

    The same endpoint used for per-keyword volume, but WITHOUT a searchTerm
    filter, which turns it from a lookup into a research feed: what people
    actually search, ranked, with a volume score, whether or not we bid on it.

    Only ~3% of our 1,152 existing keywords are ranked by Apple, so asking
    term-by-term mostly returns nothing. Reading the feed the other way round
    -- here is what IS searched -- is how you build a keyword list rather than
    grade one you already guessed.
    """
    if not legacy.ASA_V1_ENABLED:
        return {"rows": 0}
    markets = markets or DISCOVERY_MARKETS
    genres = genres or DISCOVERY_GENRES

    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=75)).replace(day=1).isoformat()
    t0, total = time.time(), 0

    for country in markets:
        for genre in genres:
            try:
                rows = legacy.asa_v1_paged(
                    "/insights/apps/search-term-popularity/query",
                    {
                        "timeRange": {"start": start, "end": today.isoformat(),
                                      "timeZone": "UTC", "granularity": "MONTHLY"},
                        "filters": [
                            {"field": "countryOrRegion", "operator": "EQUALS",
                             "value": country},
                            {"field": "genre", "operator": "EQUALS",
                             "value": genre},
                        ],
                    },
                    page_size=page_size,
                )
            except Exception as e:
                print(f"  [ads] volume feed failed for {country}/{genre}: "
                      f"{type(e).__name__}")
                continue
            payload = [{
                "month": r.get("month"),
                "country": r.get("countryOrRegion") or country,
                "genre": r.get("genre") or genre,
                "term": (r.get("searchTerm") or "").strip(),
                "rank_in_genre": r.get("rankInGenre"),
                "popularity": r.get("searchPopularity1to100"),
                "popularity_1to5": r.get("searchPopularity1to5"),
            } for r in rows if r.get("searchTerm")]
            total += store.upsert_search_term_popularity(conn, payload)
    conn.commit()
    print(f"  [ads] volume feed: {total} term-months across "
          f"{len(markets)} markets x {len(genres)} genres in {time.time()-t0:.0f}s")
    return {"rows": total}


def backfill_keyword_popularity_from_feed(conn) -> int:
    """Score our own keywords from the feed we already downloaded.

    Cheaper and more complete than asking per term: the feed is one request per
    market/genre, and a keyword that appears in it gets its volume for free.
    """
    n = conn.execute("""
        UPDATE keyword_dim SET
            popularity = (
                SELECT s.popularity FROM search_term_popularity s
                JOIN campaign_dim c ON c.campaign_id = keyword_dim.campaign_id
                WHERE LOWER(s.term) = LOWER(keyword_dim.text)
                  AND s.country = COALESCE(c.country, 'US')
                ORDER BY s.month DESC LIMIT 1),
            rank_in_genre = (
                SELECT s.rank_in_genre FROM search_term_popularity s
                JOIN campaign_dim c ON c.campaign_id = keyword_dim.campaign_id
                WHERE LOWER(s.term) = LOWER(keyword_dim.text)
                  AND s.country = COALESCE(c.country, 'US')
                ORDER BY s.month DESC LIMIT 1)
        WHERE popularity IS NULL AND text IS NOT NULL AND EXISTS (
            SELECT 1 FROM search_term_popularity s
            JOIN campaign_dim c ON c.campaign_id = keyword_dim.campaign_id
            WHERE LOWER(s.term) = LOWER(keyword_dim.text)
              AND s.country = COALESCE(c.country, 'US'))
    """).rowcount
    conn.commit()
    if n:
        print(f"  [ads] scored {n} of our keywords from the volume feed")
    return n


def main() -> int:
    conn = store.open_store()
    ingest_apple_dims(conn)
    ingest_apple(conn)
    if os.environ.get("REFRESH_POPULARITY", "1") == "1":
        # Feed first: it scores most of our keywords in a handful of requests,
        # so the per-term pass that follows has far less left to ask about.
        ingest_top_search_terms(conn)
        backfill_keyword_popularity_from_feed(conn)
        ingest_apple_popularity(conn)
    store.set_meta(conn, "ads_ingested_at_ms", store.utc_now_ms())
    conn.commit()
    st = store.store_stats(conn)
    print(f"  [ads] store: {st['ad_daily']} ad-days (latest {st['ad_daily_last']}), "
          f"{st['keywords']} keywords, {st['keywords_with_popularity']} with popularity, "
          f"{st['search_terms_ranked']} ranked terms in the volume feed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
