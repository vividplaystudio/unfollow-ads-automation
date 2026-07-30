#!/usr/bin/env python3
"""Opportunity Radar — finds NEWLY LAUNCHED apps that are ALREADY charting.

The signal: an app released in the last N days that already holds a
top-grossing (or top-free) chart position is, almost by definition, being
bought into the charts profitably. That intersection — new + already ranking —
is the whole point. Revenue estimates are NOT needed to spot it, and are
deliberately left to a manual Mobile Action check on the shortlist.

Why chart-first instead of crawling for new apps:
  Enumerating Apple's app-ID space to discover every new release is millions
  of probes against a rate-limited API. But we don't want every new app — we
  want new apps that are WINNING, and those are already in the charts. So we
  pull the charts (a few thousand IDs) and filter THOSE by release date.
  ~100 requests per sweep instead of millions, same signal.

Data sources (all free, all public, no API key):
  1. Legacy iTunes RSS charts — supports top-GROSSING and per-genre filtering,
     which the newer rss.applemarketingtools.com feed does NOT (it 404s on
     top-grossing and on genre paths). Capped at 100 entries per chart.
       https://itunes.apple.com/{cc}/rss/topgrossingapplications/limit=100/genre={g}/json
  2. iTunes Lookup API — batch metadata (up to ~100 IDs per call), returns
     releaseDate (original launch) and userRatingCount.
       https://itunes.apple.com/lookup?id=1,2,3&country={cc}

⚠️ releaseDate IS PER-STOREFRONT, NOT GLOBAL. This is the single biggest trap
in this data. The same app ID returns a different releaseDate per country —
it is the date the app became available in THAT storefront:

    SubPilot (id 6751181747)   us 2025-09-14   ca 2026-01-30
                               de 2026-03-19   gb/au 2026-06-17

Reading only the GB storefront would call a 10-month-old app "39 days old".
So after the chart sweep we run a VERIFICATION PASS across a wide set of
reference storefronts and take the EARLIEST date as the true global launch.
Soft-launch markets (nz, ie, ph, sg, za) are in that set deliberately —
developers routinely launch there months before the US.

Apps whose global launch is old but which only recently hit YOUR target
storefronts aren't discarded — they're flagged `geo_expansion`. That's a real
signal (an established app buying into your geos), just a different one from
"brand new app".

Ratings-count velocity is the free download proxy: we re-read the previous
run's output and diff userRatingCount per app. Needs 2+ runs to populate.

NOTE: the lookup API does not expose in-app-purchase price points — that is
not available for free. Check pricing manually on the shortlist.

Output: opportunity_radar.json → cPanel via FTP.
Fetch at https://genivox.com/ads-upload/opportunity_radar.json
"""

import base64
import ftplib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ══════════════════════════════════════════════════════════════════
# Config (all overridable via env / workflow inputs)
# ══════════════════════════════════════════════════════════════════

OUTPUT = "opportunity_radar.json"
NICHE_OUTPUT = "niche_clusters.json"
# Compact app_id -> chart-position index over EVERY charting app, new or not.
# The live search page cross-references against this so a result can be marked
# "actually charting at grossing #12" rather than merely "exists and is
# growing". opportunity_radar.json can't serve that — it only holds new apps,
# so incumbents would come back unmarked and look like nobodies.
INDEX_OUTPUT = "charting_index.json"
PREV_URL = os.environ.get(
    "RADAR_PREV_URL", "https://genivox.com/ads-upload/opportunity_radar.json"
)
# /ads-upload sits behind HTTP Basic Auth — same credentials the meta_history
# fetch uses (see build_ad_reference.py). Without these the previous-run read
# 401s and ratings-velocity deltas silently stay null forever.
#
# NOTE the `or` rather than a get() default throughout this block: an unset
# GitHub secret and a blank workflow input both arrive as an EMPTY STRING, not
# as absent, so get(key, default) would hand back "" and silently break things
# (blank genres = zero charts swept = an empty radar that looks like "no new
# apps found"). `or` falls through on empty too.
PREV_USER = os.environ.get("ADS_UPLOAD_USER") or "ads"
PREV_PASS = os.environ.get("ADS_UPLOAD_PASS") or "@fifi2019"

# Proven geos first, then EU + LatAm. Storefront codes are ISO-2, lowercase.
# Non-English markets are worth sweeping even if you don't advertise there yet:
# apps frequently chart in a smaller storefront weeks before the English ones,
# so they act as an early-warning net.
COUNTRIES = [c.strip().lower() for c in (
    os.environ.get("RADAR_COUNTRIES") or "us,gb,ca,au,de,fr,es,it,br,mx"
).split(",") if c.strip()]

# Meta Ad Library search — verified URL shape. Confirms whether a candidate is
# actually buying traffic, which is step 2 of the manual check.
AD_LIBRARY_URL = ("https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
                  "&country=ALL&search_type=keyword_unordered&q={q}")

# App-intelligence deep link. Mobile Action's per-app URL pattern isn't publicly
# derivable from an app ID (every documented shape 404s), so this defaults to a
# site-scoped search. Once you confirm the real pattern from a logged-in
# session, set RADAR_INTEL_URL with {id} / {name} placeholders.
INTEL_URL = os.environ.get("RADAR_INTEL_URL") or "https://www.google.com/search?q=site:mobileaction.co+{name}"

# Storefronts checked ONLY to establish the true global launch date (see the
# releaseDate warning in the module docstring). Includes the classic
# soft-launch markets — nz/ie/ph/sg/za often predate the US launch by months,
# and missing them would let an old app masquerade as new.
REFERENCE_COUNTRIES = [c.strip().lower() for c in (
    os.environ.get("RADAR_REFERENCE_COUNTRIES")
    or "us,gb,ca,au,nz,ie,de,fr,jp,br,in,mx,sg,ph,za,kr,it,es"
).split(",") if c.strip()]

# Games (6014) deliberately excluded — different business, different playbook.
GENRE_NAMES = {
    "6000": "Business",
    "6002": "Utilities",
    "6003": "Travel",
    "6005": "Social Networking",
    "6006": "Reference",
    "6007": "Productivity",
    "6008": "Photo & Video",
    "6012": "Lifestyle",
    "6013": "Health & Fitness",
    "6015": "Finance",
    "6016": "Entertainment",
    "6017": "Education",
    "6023": "Food & Drink",
    "6024": "Shopping",
}
GENRES = [g.strip() for g in (os.environ.get("RADAR_GENRES") or ",".join(GENRE_NAMES)).split(",") if g.strip()]

# top-grossing is the money signal; top-free catches paid-UA pushes that
# haven't converted to revenue rank yet.
CHARTS = {
    "grossing": "topgrossingapplications",
    "free": "topfreeapplications",
}
ACTIVE_CHARTS = [c.strip() for c in (os.environ.get("RADAR_CHARTS") or "grossing,free").split(",") if c.strip()]

MAX_AGE_DAYS = int(os.environ.get("RADAR_MAX_AGE_DAYS") or 180)
CHART_LIMIT = int(os.environ.get("RADAR_CHART_LIMIT") or 100)  # Apple caps at 100
LOOKUP_BATCH = int(os.environ.get("RADAR_LOOKUP_BATCH") or 100)
THROTTLE = float(os.environ.get("RADAR_THROTTLE") or 0.4)  # seconds between requests

FTP_HOST = os.environ.get("FTP_HOST", "")
FTP_USER = os.environ.get("FTP_USER", "")
FTP_PASS = os.environ.get("FTP_PASS", "")
FTP_PATH = os.environ.get("FTP_PATH", "")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# ══════════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════════

def get_json(url: str, retries: int = 3, auth: tuple = None) -> dict:
    """GET + parse JSON, retrying transient 5xx (Apple's feeds 504 sporadically)."""
    last = None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            raise
        except Exception as e:  # noqa: BLE001 — network flake, back off and retry
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last})")


# ══════════════════════════════════════════════════════════════════
# Step 1 — charts
# ══════════════════════════════════════════════════════════════════

def fetch_chart(country: str, chart_key: str, genre: str) -> list:
    """Return [(app_id, rank)] for one country × chart × genre."""
    feed = CHARTS[chart_key]
    url = f"https://itunes.apple.com/{country}/rss/{feed}/limit={CHART_LIMIT}/genre={genre}/json"
    try:
        data = get_json(url)
    except Exception as e:  # noqa: BLE001 — one dead chart must not kill the sweep
        print(f"    ! {country}/{chart_key}/{genre} ERROR: {e}")
        return []
    entries = data.get("feed", {}).get("entry", [])
    if isinstance(entries, dict):  # Apple collapses a 1-item feed to an object
        entries = [entries]
    out = []
    for rank, e in enumerate(entries, start=1):
        try:
            out.append((e["id"]["attributes"]["im:id"], rank))
        except (KeyError, TypeError):
            continue
    return out


def sweep_charts() -> dict:
    """{country: {app_id: [{chart, genre, genre_name, rank}, ...]}}"""
    by_country = {}
    for country in COUNTRIES:
        seen = {}
        print(f"\n── {country.upper()} ──")
        for chart_key in ACTIVE_CHARTS:
            hits = 0
            for genre in GENRES:
                for app_id, rank in fetch_chart(country, chart_key, genre):
                    seen.setdefault(app_id, []).append({
                        "chart": chart_key,
                        "genre_id": genre,
                        "genre": GENRE_NAMES.get(genre, genre),
                        "rank": rank,
                    })
                    hits += 1
                time.sleep(THROTTLE)
            print(f"  {chart_key:9} {hits} chart slots across {len(GENRES)} genres")
        by_country[country] = seen
        print(f"  → {len(seen)} distinct apps")
    return by_country


# ══════════════════════════════════════════════════════════════════
# Step 2 — metadata
# ══════════════════════════════════════════════════════════════════

def lookup_batch(app_ids: list, country: str) -> dict:
    """{app_id: metadata} — batched, so ~1 call per 100 apps."""
    out = {}
    for i in range(0, len(app_ids), LOOKUP_BATCH):
        chunk = app_ids[i:i + LOOKUP_BATCH]
        url = f"https://itunes.apple.com/lookup?id={','.join(chunk)}&country={country}&entity=software"
        try:
            data = get_json(url)
        except Exception as e:  # noqa: BLE001
            print(f"    ! lookup {country} chunk {i // LOOKUP_BATCH} ERROR: {e}")
            continue
        for r in data.get("results", []):
            tid = str(r.get("trackId", ""))
            if tid:
                out[tid] = r
        time.sleep(THROTTLE)
    return out


# ══════════════════════════════════════════════════════════════════
# Step 3 — previous run (for ratings velocity + first_seen)
# ══════════════════════════════════════════════════════════════════

def priority_score(rec: dict) -> float:
    """0–100 ranking heuristic. Sorting on grossing rank alone treats a static
    #20 the same as one that climbed from #80 overnight, and treats a
    single-country #5 the same as a #5 charting in six. This weights the things
    that actually predict a winner. The breakdown is stored on the record so
    every number is auditable rather than a black box.
    """
    b = {}
    # Money signal. Rank 1 → 40 pts, rank 100 → ~0.
    g = rec["best_grossing_rank"]
    b["grossing"] = round(40 * (1 - (g - 1) / 100), 1) if g else 0.0
    # Multi-geo. Charting in many storefronts means sustained real spend,
    # not a one-market fluke.
    b["geo"] = round(min(len(rec["countries"]), 6) / 6 * 20, 1)
    # Momentum. Only upward movement scores — a faller gets 0, not a penalty,
    # since a high static rank is still a valid signal.
    d = rec.get("grossing_rank_delta") or 0
    b["momentum"] = round(min(max(d, 0), 50) / 50 * 20, 1)
    # Youth. Catching it at week 3 beats month 5.
    b["youth"] = round(max(0, (MAX_AGE_DAYS - rec["days_since_launch"]) / MAX_AGE_DAYS) * 15, 1)
    # Download signal — small bonus for also holding a top-free position.
    f = rec["best_free_rank"]
    b["free"] = round(5 * (1 - (f - 1) / 100), 1) if f else 0.0

    rec["score_breakdown"] = b
    return round(sum(b.values()), 1)


# Generic marketing words that appear across unrelated apps. Without this list
# "universal", "fast" and "smart" outrank real niches purely on frequency.
# Expect to tune it once you've seen a few runs — override via RADAR_STOPWORDS.
NICHE_STOPWORDS = set((os.environ.get("RADAR_STOPWORDS") or (
    "app,apps,free,pro,plus,premium,lite,the,and,for,with,your,you,my,our,all,new,best,top,easy,"
    "simple,quick,fast,smart,super,ultra,max,mini,hd,now,one,two,get,make,made,use,using,ios,"
    "iphone,ipad,mobile,phone,universal,official,daily,real,true,live,plus,and,its,que,para,con,"
    "der,die,das,und,fur,mit,von,dem,les,des,une,pour,avec,sur,por,com,que,dos,das,edition,version"
)).split(","))
NICHE_STOPWORDS = {w.strip() for w in NICHE_STOPWORDS if w.strip()}

MIN_NICHE_PUBLISHERS = int(os.environ.get("RADAR_MIN_NICHE_PUBLISHERS") or 3)

# Niche-entrant probe: how many niches to search, and in which storefront.
# One extra API call per niche, so this is the only knob that costs anything.
NICHE_SEARCH_TOP = int(os.environ.get("RADAR_NICHE_SEARCH_TOP") or 40)
SEARCH_COUNTRY = (os.environ.get("RADAR_SEARCH_COUNTRY") or "us").lower()


def probe_niche_entrants(term: str, now) -> dict:
    """Find apps that ENTERED a niche recently, charting or not.

    The chart sweep only sees the top 100 per category, so a newcomer sitting
    at rank 250 is invisible to it. The Search API queries the whole App Store
    by keyword, which is how you tell "nobody has tried this niche lately" from
    "lots of people tried and none broke through".

    Traction is measured as RATINGS PER DAY, not chart position, and that
    distinction is the point. An app can be bought to real scale with paid UA
    while never cracking a top-100 grossing chart — and in ad-driven niches
    (TV remotes being the textbook case) that is the normal path. Judging
    entrants purely on whether they charted would score a well-funded paid-UA
    launch as a failure. Ratings/day catches traction regardless of HOW it was
    acquired, which is the only fair read when the winning strategy is ads
    rather than ASO.
    """
    url = (f"https://itunes.apple.com/search?term={urllib.parse.quote_plus(term)}"
           f"&country={SEARCH_COUNTRY}&entity=software&limit=200")
    try:
        data = get_json(url)
    except Exception as e:  # noqa: BLE001 — one failed probe must not kill the run
        return {"searched": False, "error": str(e)}

    entrants = []
    for r in data.get("results", []):
        rel = r.get("releaseDate")
        if not rel:
            continue
        try:
            age = (now - datetime.fromisoformat(rel.replace("Z", "+00:00"))).days
        except ValueError:
            continue
        if age > MAX_AGE_DAYS or age < 0:
            continue
        ratings = r.get("userRatingCount") or 0
        entrants.append({
            "name": r.get("trackName"),
            "publisher": r.get("sellerName") or r.get("artistName"),
            "url": r.get("trackViewUrl"),
            "icon": r.get("artworkUrl100"),
            "age_days": age,
            "ratings_count": ratings,
            "ratings_per_day": round(ratings / max(age, 1), 2),
            "ad_library_url": AD_LIBRARY_URL.format(q=urllib.parse.quote_plus(r.get("trackName") or "")),
        })

    entrants.sort(key=lambda x: -x["ratings_per_day"])
    best = entrants[0]["ratings_per_day"] if entrants else 0.0
    return {
        "searched": True,
        "attempts": len(entrants),
        "best_traction": best,
        "with_traction": sum(1 for e in entrants if e["ratings_per_day"] >= 1),
        # Read deliberately in terms of demonstrated traction, NOT chart rank —
        # see the docstring. "Nobody through" means nobody has cracked it
        # recently by any route; it does NOT mean the niche is unwinnable,
        # since most entrants never spend on acquisition at all.
        "verdict": ("proven_enterable" if best >= 5
                    else "some_movement" if best >= 1
                    else "nobody_through" if len(entrants) >= 5
                    else "uncontested"),
        "entrants": entrants[:10],
    }


def build_niches(all_charting: dict, new_ids: set) -> list:
    """Cluster ALL charting apps by shared name terms within a category.

    Separate question from the new-app radar. That one asks "is someone winning
    right now?"; this asks "can this market support more than one winner?" —
    which is why it deliberately ignores age and includes incumbents.

    DISTINCT PUBLISHERS is the metric, not app count. 15 publishers each with a
    charting remote app means 15 viable businesses. 15 apps from 2 publishers
    means two operators shipping clones — a completely different, far weaker
    signal that an app count alone would not distinguish.

    Name-token matching is crude and will miss same-niche apps with dissimilar
    names ("Unfollow Tracker" vs "Ghost Detector" share no words). It is a lead
    generator, not a taxonomy — the app lists are shown so you can eyeball
    whether a cluster is real.
    """
    clusters = {}
    for aid, a in all_charting.items():
        name = (a["name"] or "").lower()
        words = [w for w in re.findall(r"[a-z]{3,}", name) if w not in NICHE_STOPWORDS]
        # Bigrams as well as single words: "screen mirroring" is a niche,
        # "screen" and "mirroring" separately are much weaker terms.
        terms = set(words) | {f"{x} {y}" for x, y in zip(words, words[1:])}
        genre = a["primary_genre"] or "Unknown"
        for t in terms:
            c = clusters.setdefault((genre, t), {
                "term": t, "category": genre, "apps": [], "publishers": set(),
            })
            c["apps"].append(a)
            if a["publisher"]:
                c["publishers"].add(a["publisher"])

    # DEDUPE OVERLAPPING CLUSTERS. Token matching produces the same niche under
    # several words — "storage", "clean storage" and "storage cleaner" were
    # three separate entries covering an identical app set, and 22 of 35 niches
    # in one test were near-duplicates of another. Greedily keep the strongest
    # cluster and absorb anything that substantially overlaps it.
    #
    # Strongest = most publishers, tie-broken by the LONGER term, so "remote
    # control" survives over the vaguer "control" and "screen mirroring" over
    # bare "screen" — the bigram is nearly always the more meaningful niche.
    survivors = []
    order = sorted(
        (c for c in clusters.values() if len(c["publishers"]) >= MIN_NICHE_PUBLISHERS),
        key=lambda c: (-len(c["publishers"]), -len(c["term"])),
    )
    for c in order:
        ids = {a["app_id"] for a in c["apps"]}
        dup_of = None
        for s in survivors:
            if s["category"] != c["category"]:
                continue
            overlap = len(ids & s["_ids"]) / min(len(ids), len(s["_ids"]))
            if overlap >= 0.7:
                dup_of = s
                break
        if dup_of:
            dup_of["_aliases"].append(c["term"])
        else:
            c["_ids"] = ids
            c["_aliases"] = []
            survivors.append(c)

    out = []
    for c in survivors:
        genre, term = c["category"], c["term"]
        apps = c["apps"]
        grossing = [a["grossing"] for a in apps if a["grossing"]]
        new_entrants = [a for a in apps if a["app_id"] in new_ids]
        best = min(grossing) if grossing else None

        # Market depth — how many independent operators sustain a business here.
        depth = min(len(c["publishers"]), 15) / 15 * 45
        # Is there actual Apple-processed money in this niche?
        money = 35 * (1 - (best - 1) / 100) if best else 0.0
        # Openness — are recent entrants ALSO charting, or is it locked up by
        # incumbents? This is the factor that decides whether you can get in.
        openness = min(len(new_entrants), 4) / 4 * 20

        out.append({
            "term": term,
            "aliases": sorted(set(c["_aliases"]))[:6],
            "category": genre,
            "app_count": len(apps),
            "publisher_count": len(c["publishers"]),
            "new_entrant_count": len(new_entrants),
            "grossing_count": len(grossing),
            "best_grossing_rank": best,
            "median_age_days": sorted(a["age_days"] for a in apps)[len(apps) // 2],
            "score": round(depth + money + openness, 1),
            "score_breakdown": {"depth": round(depth, 1), "money": round(money, 1),
                                "openness": round(openness, 1)},
            "examples": [
                {"name": a["name"], "url": a["url"], "icon": a["icon"],
                 "publisher": a["publisher"], "grossing_rank": a["grossing"],
                 "age_days": a["age_days"], "is_new": a["app_id"] in new_ids,
                 "countries": sorted(a["countries"])}
                for a in sorted(apps, key=lambda x: (x["grossing"] is None, x["grossing"] or 9999))[:12]
            ],
        })

    return sorted(out, key=lambda x: -x["score"])


def load_previous() -> dict:
    """{app_id: prev_record} from the last published run. Empty on first run."""
    try:
        data = get_json(PREV_URL, retries=1, auth=(PREV_USER, PREV_PASS))
        prev = {a["app_id"]: a for a in data.get("apps", [])}
        print(f"  loaded {len(prev)} apps from previous run")
        return prev
    except Exception as e:  # noqa: BLE001 — first run has nothing to load
        print(f"  no previous run available ({e})")
        return {}


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

def main() -> None:
    now = datetime.now(timezone.utc)
    print(f"▶ Opportunity Radar  {now.date()}")
    print(f"  countries: {COUNTRIES}")
    print(f"  genres:    {len(GENRES)}  charts: {ACTIVE_CHARTS}")
    print(f"  window:    apps released in the last {MAX_AGE_DAYS} days")

    print("\n▶ Loading previous run")
    prev = load_previous()

    print("\n▶ Sweeping charts")
    charts_by_country = sweep_charts()

    print("\n▶ Fetching metadata")
    apps = {}
    all_charting = {}
    for country, id_map in charts_by_country.items():
        meta = lookup_batch(list(id_map.keys()), country)
        print(f"  {country.upper()}: resolved {len(meta)}/{len(id_map)}")

        for app_id, placements in id_map.items():
            m = meta.get(app_id)
            if not m:
                continue

            released = m.get("releaseDate")
            if not released:
                continue
            try:
                rel_dt = datetime.fromisoformat(released.replace("Z", "+00:00"))
            except ValueError:
                continue

            # Retain EVERY charting app — not just the new ones — for the niche
            # clustering below. This metadata is already fetched and was
            # previously discarded at the age filter, so keeping it costs zero
            # extra API calls. A niche's depth is measured across incumbents,
            # which are by definition the old apps.
            ch = all_charting.setdefault(app_id, {
                "app_id": app_id,
                "name": m.get("trackName"),
                "publisher": m.get("sellerName") or m.get("artistName"),
                "primary_genre": m.get("primaryGenreName"),
                "icon": m.get("artworkUrl100"),
                "url": m.get("trackViewUrl"),
                "ratings_count": 0,
                "age_days": (now - rel_dt).days,
                "countries": set(),
                "grossing": None,
                "free": None,
            })
            # ⚠️ userRatingCount IS PER-STOREFRONT. SAP Concur reports 1,128,139
            # in one market and 139,654 in another. Taking whichever country's
            # lookup reached the app first made the value depend on iteration
            # order and on which charts it happened to appear in that run — so
            # when that flipped between runs the velocity diff read as
            # +988,485 ratings in one hour (3.9M/day). Always take the MAX, so
            # the figure is stable across runs regardless of chart membership.
            # Provisional only — the fixed-storefront pass below overwrites
            # this. Kept so the field always exists if that pass fails.
            ch["ratings_count"] = max(ch["ratings_count"], m.get("userRatingCount") or 0)
            ch["countries"].add(country)
            for p in placements:
                key = "grossing" if p["chart"] == "grossing" else "free"
                if ch[key] is None or p["rank"] < ch[key]:
                    ch[key] = p["rank"]

            # CHEAP PRE-FILTER on the storefront-local date. Safe to use here:
            # the true global date is always <= the earliest local date we see,
            # so anything already too old locally is definitely too old
            # globally. The reverse is NOT true, which is what the verification
            # pass below exists to catch.
            age_days = (now - rel_dt).days
            if age_days > MAX_AGE_DAYS or age_days < 0:
                continue

            rec = apps.setdefault(app_id, {
                "app_id": app_id,
                "name": m.get("trackName"),
                "publisher": m.get("sellerName") or m.get("artistName"),
                "bundle_id": m.get("bundleId"),
                "primary_genre": m.get("primaryGenreName"),
                "genres": m.get("genres", []),
                "price": m.get("formattedPrice"),
                "icon": m.get("artworkUrl100"),
                # Developer's App Store page — lists every app on that account,
                # which is how you go from "this publisher has 4 charting" to
                # actually seeing their whole portfolio.
                "artist_id": str(m.get("artistId") or ""),
                "artist_url": m.get("artistViewUrl"),
                "last_updated": (m.get("currentVersionReleaseDate") or "")[:10],
                "rating": round(m.get("averageUserRating") or 0, 2),
                "ratings_count": m.get("userRatingCount") or 0,
                "url": m.get("trackViewUrl"),
                "countries": [],
                "placements": [],
                "storefront_releases": {},
            })
            rec["storefront_releases"][country] = released[:10]
            if country not in rec["countries"]:
                rec["countries"].append(country)
            for p in placements:
                rec["placements"].append({**p, "country": country})

    # ── Verification pass: find the TRUE global launch date ──────────
    # Without this, an app that launched in the US last year but only reached
    # GB last month reads as "30 days old". Batched per storefront, so this is
    # ~4 calls per reference country regardless of candidate count.
    print(f"\n▶ Verifying global launch dates across {len(REFERENCE_COUNTRIES)} storefronts")
    candidates = list(apps.keys())
    print(f"  {len(candidates)} candidates to verify")
    for ref in REFERENCE_COUNTRIES:
        meta = lookup_batch(candidates, ref)
        found = 0
        for app_id, m in meta.items():
            rel = m.get("releaseDate")
            if rel and app_id in apps:
                apps[app_id]["storefront_releases"][ref] = rel[:10]
                found += 1
        print(f"  {ref.upper()}: {found}/{len(candidates)}")

    # Collapse to the earliest date seen anywhere = the real launch.
    aged_out = 0
    for app_id in list(apps.keys()):
        rec = apps[app_id]
        dates = rec["storefront_releases"]
        first_date = min(dates.values())
        rec["released"] = first_date
        rec["days_since_launch"] = (now - datetime.fromisoformat(first_date + "T00:00:00+00:00")).days

        # Most apps go live everywhere on the same day, so "the first market"
        # is usually meaningless — naively taking the first tied country just
        # reports whichever sorts alphabetically (always "au"). Only call out a
        # first market when the launch was genuinely staggered.
        earliest = sorted(c for c, d in dates.items() if d == first_date)
        rec["first_markets"] = earliest
        rec["launch_type"] = "global" if len(earliest) > 3 else "staggered"
        rec["first_market"] = "global" if len(earliest) > 3 else ",".join(earliest)

        # Local date in the swept geos — how long it's been live where YOU sell.
        local = [d for c, d in dates.items() if c in COUNTRIES]
        rec["released_local"] = min(local) if local else first_date

        if rec["days_since_launch"] > MAX_AGE_DAYS:
            del apps[app_id]  # old globally — was a geo expansion, not a launch
            aged_out += 1
            continue

        # Genuinely new globally, but reached the target geos much later.
        rec["geo_expansion"] = rec["released_local"] > rec["released"]

    print(f"  ✂ removed {aged_out} apps that were older than they looked "
          f"(geo expansions, not launches)")

    # Derived fields: best ranks, ratings velocity, first_seen
    for app_id, rec in apps.items():
        grossing = [p["rank"] for p in rec["placements"] if p["chart"] == "grossing"]
        free = [p["rank"] for p in rec["placements"] if p["chart"] == "free"]
        rec["best_grossing_rank"] = min(grossing) if grossing else None
        rec["best_free_rank"] = min(free) if free else None
        rec["chart_appearances"] = len(rec["placements"])

        # Deep links for the manual verification step.
        q = urllib.parse.quote_plus(rec["name"] or "")
        rec["ad_library_url"] = AD_LIBRARY_URL.format(q=q)
        rec["intel_url"] = INTEL_URL.replace("{id}", rec["app_id"]).replace("{name}", q)

        p = prev.get(app_id)
        if p:
            rec["first_seen"] = p.get("first_seen", now.date().isoformat())
            rec["is_new"] = False
            delta = rec["ratings_count"] - (p.get("ratings_count") or 0)
            rec["ratings_delta"] = delta
            # Rough install proxy: ratings are typically ~1-2% of installs.
            rec["est_daily_installs_low"] = int(delta / 0.02) if delta > 0 else 0
            rec["est_daily_installs_high"] = int(delta / 0.01) if delta > 0 else 0

            # Rank momentum. POSITIVE = climbing (rank number went DOWN), which
            # is the direction that matters — a mover is spending effectively
            # right now, where a static rank is just a snapshot.
            for key, field in (("best_grossing_rank", "grossing_rank_delta"),
                               ("best_free_rank", "free_rank_delta")):
                cur, was = rec[key], p.get(key)
                rec[field] = (was - cur) if (cur and was) else None
        else:
            rec["first_seen"] = now.date().isoformat()
            rec["is_new"] = True
            rec["grossing_rank_delta"] = None
            rec["free_rank_delta"] = None
            rec["ratings_delta"] = None
            rec["est_daily_installs_low"] = None
            rec["est_daily_installs_high"] = None

    # Publisher portfolio. A publisher with several apps charting at once has a
    # repeatable formula — those operators are worth studying more than any
    # single app they've shipped.
    portfolio = {}
    for rec in apps.values():
        if rec["publisher"]:
            portfolio.setdefault(rec["publisher"], []).append(rec)
    for rec in apps.values():
        rec["publisher_app_count"] = len(portfolio.get(rec["publisher"], [])) or 1

    # Score last — it reads best_*_rank, the momentum deltas, and countries,
    # so every input has to be populated first.
    for rec in apps.values():
        rec["score"] = priority_score(rec)

    ranked = sorted(apps.values(), key=lambda a: (-a["score"], a["best_grossing_rank"] or 9999))

    # Built after scoring so each entry can carry its apps' ranks and scores.
    # Grouped by publisher NAME, not artist ID — an operator running several
    # App Store accounts would otherwise be split into separate rows and the
    # portfolio pattern would be invisible, which is the thing worth spotting.
    # Each app keeps its own link, so a group spanning accounts still works.
    multi = sorted(
        ({"publisher": p,
          "app_count": len(recs),
          "artist_url": next((r["artist_url"] for r in recs if r.get("artist_url")), None),
          "apps": [{"name": r["name"], "url": r["url"], "icon": r["icon"],
                    "grossing_rank": r["best_grossing_rank"], "score": r["score"],
                    "days": r["days_since_launch"]}
                   for r in sorted(recs, key=lambda r: -r["score"])]}
         for p, recs in portfolio.items() if len(recs) > 1),
        key=lambda x: (-x["app_count"], -max(a["score"] for a in x["apps"])),
    )

    payload = {
        "generated_at": now.isoformat(),
        "config": {
            "countries": COUNTRIES,
            "reference_countries": REFERENCE_COUNTRIES,
            "genres": {g: GENRE_NAMES.get(g, g) for g in GENRES},
            "charts": ACTIVE_CHARTS,
            "max_age_days": MAX_AGE_DAYS,
            "chart_limit": CHART_LIMIT,
        },
        "counts": {
            "total": len(ranked),
            "grossing_ranked": sum(1 for a in ranked if a["best_grossing_rank"]),
            "under_90_days": sum(1 for a in ranked if a["days_since_launch"] <= 90),
            "under_30_days": sum(1 for a in ranked if a["days_since_launch"] <= 30),
            "geo_expansions": sum(1 for a in ranked if a.get("geo_expansion")),
            "removed_as_older_than_they_looked": aged_out,
            "new_since_last_run": sum(1 for a in ranked if a.get("is_new")),
            "climbing": sum(1 for a in ranked if (a.get("grossing_rank_delta") or 0) > 0),
            "multi_app_publishers": len(multi),
        },
        "multi_app_publishers": multi,
        "apps": ranked,
    }

    with open(OUTPUT, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    # ── Stable ratings pass ──────────────────────────────────────────
    # Ratings MUST come from a fixed storefront or velocity is meaningless.
    # Taking max() across the countries an app charted in was still unstable:
    # when an app enters a new, larger storefront's charts its max jumps, and
    # that reads as explosive growth. Setmore showed 7,716/day purely because
    # it went from charting in GB (1,540 ratings) to charting in the US (9,175).
    #
    # So ask a FIXED priority of storefronts, first hit wins, and record WHICH
    # one answered. A delta is only computed when the same storefront answered
    # both runs — see the rc check below.
    print("\n▶ Reading ratings from a fixed storefront (for stable velocity)")
    RATINGS_PRIORITY = [c for c in ("us", "gb", "de", "fr", "jp") if c]
    pending = set(all_charting)
    for cc in RATINGS_PRIORITY:
        if not pending:
            break
        ids = list(pending)
        got = 0
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            url = (f"https://itunes.apple.com/lookup?id={','.join(chunk)}"
                   f"&country={cc}&entity=software")
            try:
                res = get_json(url, retries=1).get("results", [])
            except Exception:  # noqa: BLE001
                continue
            for r in res:
                aid = str(r.get("trackId") or "")
                if aid in pending and r.get("kind") == "software":
                    all_charting[aid]["ratings_count"] = r.get("userRatingCount") or 0
                    all_charting[aid]["ratings_country"] = cc
                    pending.discard(aid)
                    got += 1
            time.sleep(THROTTLE)
        print(f"  {cc.upper()}: {got} apps  ({len(pending)} still unresolved)")
    for aid in pending:  # not sold in any priority storefront
        all_charting[aid]["ratings_country"] = "?"

    # ── Charting index (feeds the live search page) ──────────────────
    # Keys are terse (g/f/c/n) purely for size — this is ~8k apps and the
    # browser fetches it on every page load.
    # Diff ratings against the previous index to get TRUE current velocity.
    # ratings/day computed from lifetime totals is an average — an app that was
    # hot six months ago and is flat now scores the same as one growing today.
    # This delta is actual movement since the last run.
    prev_index, pj = {}, {}
    prev_at = None
    try:
        pj = get_json(PREV_URL.rsplit("/", 1)[0] + "/" + INDEX_OUTPUT,
                      retries=1, auth=(PREV_USER, PREV_PASS))
        prev_index = pj.get("apps", {})
        prev_at = pj.get("generated_at")
        print(f"  previous index: {len(prev_index)} apps from {(prev_at or '')[:10]}")
    except Exception as e:  # noqa: BLE001 — first run has none
        print(f"  no previous index ({e})")

    # The baseline is held SEPARATE from the current reading and only rolls
    # forward once enough time has passed. Naively diffing against "whatever
    # the last run wrote" breaks the moment you run twice in an evening: Apple
    # refreshes userRatingCount periodically, not live (30 of the 30 largest
    # apps — YouTube included — gained exactly zero over 18 minutes), so a
    # short re-run produces an all-zero diff AND overwrites the only useful
    # baseline. Carrying the older baseline through short gaps means manual
    # re-runs no longer destroy the comparison window.
    MIN_BASELINE_DAYS = 0.5
    prev_baseline_at = (pj.get("baseline_at") or prev_at) if prev_index else None
    span_days = None
    if prev_baseline_at:
        try:
            span_days = (now - datetime.fromisoformat(
                prev_baseline_at.replace("Z", "+00:00"))).total_seconds() / 86400
        except ValueError:
            span_days = None

    roll = span_days is None or span_days >= MIN_BASELINE_DAYS
    baseline_at = now.isoformat() if roll else prev_baseline_at

    index, gained, carried = {}, 0, 0
    for aid, a in all_charting.items():
        cur = a["ratings_count"]
        rec = {"g": a["grossing"], "f": a["free"], "c": len(a["countries"]),
               "n": a["name"], "r": cur, "rc": a.get("ratings_country", "?")}
        p = prev_index.get(aid) or {}
        # Prefer the explicit baseline; fall back to the previous reading for
        # indexes written before this field existed.
        base = p.get("b", p.get("r"))
        rec["b"] = cur if roll else (base if base is not None else cur)
        # Require a real measurement window. Extrapolating an hour's noise to a
        # daily rate multiplied it by 4 and turned rounding into headline
        # numbers; below a quarter-day there is nothing meaningful to divide.
        # Same storefront both runs, or the comparison is apples-to-oranges.
        same_store = p.get("rc") == rec["rc"] and rec["rc"] != "?"
        if base is not None and span_days and span_days >= 0.25 and same_store:
            delta = cur - base
            # No genuine app gains 10k ratings a day. Anything above that is a
            # data artefact (a storefront switch, an ID reused), not growth.
            if 0 < delta / span_days <= 10_000:
                rec["d"] = round(delta / span_days, 1)
                gained += 1
        elif p.get("d") is not None:
            # Window too short to measure — CARRY the previous reading rather
            # than dropping it. Declining to compute is correct; discarding the
            # last valid measurement is not. A manual run at 08:15 produced real
            # velocity over a 15h window, then the cron fired at 08:20 and blanked
            # every number because five minutes is unmeasurable. Stale figures
            # beat none until a long enough window comes round again.
            rec["d"] = p["d"]
            carried += 1
        index[aid] = rec

    with open(INDEX_OUTPUT, "w") as f:
        json.dump({"generated_at": now.isoformat(), "baseline_at": baseline_at,
                   "countries": COUNTRIES,
                   "span_days": round(span_days, 2) if span_days else None,
                   "count": len(index), "with_velocity": gained, "apps": index},
                  f, separators=(",", ":"), default=str)
    span_txt = f"{span_days:.2f}d" if span_days else "no baseline yet"
    carry_txt = f", {carried} carried from the last valid window" if carried else ""
    print(f"✓ {INDEX_OUTPUT}: {len(index)} charting apps indexed "
          f"({gained} with velocity over {span_txt}{carry_txt}"
          f"{'' if roll else ' — baseline carried, gap too short to roll'})")

    # ── Niche clusters (separate deliverable, same run) ──────────────
    print(f"\n▶ Clustering niches across {len(all_charting)} charting apps "
          f"(incumbents included — costs no extra API calls)")
    niches = build_niches(all_charting, set(apps.keys()))

    # Probe the top niches for recent entrants the charts can't see.
    top = niches[:NICHE_SEARCH_TOP]
    print(f"  probing {len(top)} niches for off-chart entrants ({SEARCH_COUNTRY.upper()})")
    for n in top:
        n["entrant_probe"] = probe_niche_entrants(n["term"], now)
        time.sleep(THROTTLE)
    probed = [n for n in top if n["entrant_probe"].get("searched")]
    print(f"  probed {len(probed)}: "
          f"{sum(1 for n in probed if n['entrant_probe']['verdict'] == 'proven_enterable')} proven enterable, "
          f"{sum(1 for n in probed if n['entrant_probe']['verdict'] == 'nobody_through')} nobody through")

    niche_payload = {
        "generated_at": now.isoformat(),
        "config": {
            "countries": COUNTRIES,
            "min_publishers": MIN_NICHE_PUBLISHERS,
            "apps_analysed": len(all_charting),
            "new_app_window_days": MAX_AGE_DAYS,
        },
        "counts": {
            "niches": len(niches),
            "with_new_entrants": sum(1 for n in niches if n["new_entrant_count"]),
            "deep_markets": sum(1 for n in niches if n["publisher_count"] >= 8),
            "probed": len(probed),
            "proven_enterable": sum(1 for n in probed
                                    if n["entrant_probe"]["verdict"] == "proven_enterable"),
            "uncontested": sum(1 for n in probed
                               if n["entrant_probe"]["verdict"] == "uncontested"),
        },
        "niches": niches,
    }
    with open(NICHE_OUTPUT, "w") as f:
        json.dump(niche_payload, f, indent=2, default=str)
    nc = niche_payload["counts"]
    print(f"✓ {NICHE_OUTPUT}: {nc['niches']} niches "
          f"({nc['with_new_entrants']} with new entrants charting, "
          f"{nc['deep_markets']} with 8+ publishers)")

    c = payload["counts"]
    print(f"\n✓ {OUTPUT}: {c['total']} genuinely-new-and-charting apps "
          f"({c['grossing_ranked']} grossing-ranked, {c['under_30_days']} launched <30d, "
          f"{c['new_since_last_run']} new since last run, {c['climbing']} climbing)")

    # UPLOAD BEFORE PRETTY-PRINTING. The summary below is cosmetic; the upload
    # is the entire point of the run. A formatting bug in the summary once
    # crashed the process here and silently skipped the upload, so the ordering
    # is deliberate — never put anything fallible between the data being ready
    # and the data being shipped.
    upload_to_ftp(OUTPUT, OUTPUT)
    upload_to_ftp(NICHE_OUTPUT, NICHE_OUTPUT)
    upload_to_ftp(INDEX_OUTPUT, INDEX_OUTPUT)

    # HTML pages are NOT uploaded here — upload-dashboard.yml owns them.
    #
    # This script used to ship them too, which raced: a radar run started at
    # 17:33 on one commit finished at 17:46:48 and overwrote the fixed
    # search.html that upload-dashboard had written at 17:46:12 from a NEWER
    # commit. The long-running job wins simply by finishing last, so a page fix
    # could silently revert minutes after deploying. One uploader per artifact:
    # this script owns the JSON, upload-dashboard.yml owns the pages.

    try:
        print("\n── Top 15 by priority score ──")
        for a in ranked[:15]:
            g = f"#{a['best_grossing_rank']}" if a["best_grossing_rank"] else "—"
            d = a.get("grossing_rank_delta")
            mom = f"▲{d}" if d and d > 0 else (f"▼{abs(d)}" if d and d < 0 else "")
            tag = "NEW" if a.get("is_new") else ""
            pub = f"×{a['publisher_app_count']}" if a["publisher_app_count"] > 1 else ""
            print(f"  {a['score']:>5.1f}  gross {g:>5} {mom:>5}  {a['days_since_launch']:>4}d  "
                  f"{len(a['countries'])}geo {tag:3} {pub:3}  "
                  f"{(a['name'] or '')[:32]:32}  {(a['publisher'] or '')[:20]}")

        if multi:
            print("\n── Publishers with multiple apps charting ──")
            for p in multi[:8]:
                names = ", ".join(x["name"] or "" for x in p["apps"])
                print(f"  {p['app_count']}×  {(p['publisher'] or '')[:34]:34}  {names[:60]}")
    except Exception as e:  # noqa: BLE001 — console output must never fail the run
        print(f"  ! summary print failed ({e}) — data already uploaded, ignoring")


def upload_to_ftp(local_file: str, remote_name: str) -> None:
    if not FTP_HOST:
        print("  [skip FTP — no credentials]")
        return

    print(f"  Uploading to {FTP_HOST}:{FTP_PATH}/{remote_name}")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    ftp = ftplib.FTP_TLS(FTP_HOST, timeout=60, context=ctx)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.prot_p()

    try:
        ftp.cwd(FTP_PATH)
    except ftplib.error_perm:
        parts = FTP_PATH.strip("/").split("/")
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else f"/{part}"
            try:
                ftp.cwd(current)
            except ftplib.error_perm:
                ftp.mkd(current)
                ftp.cwd(current)

    with open(local_file, "rb") as f:
        ftp.storbinary(f"STOR {remote_name}", f)
    ftp.quit()
    print(f"    ✅ Uploaded {remote_name}")


if __name__ == "__main__":
    main()
