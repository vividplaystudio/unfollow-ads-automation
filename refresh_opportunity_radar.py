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

    # Ship the viewer alongside the data. It must sit in the SAME folder so its
    # relative fetch("opportunity_radar.json") inherits /ads-upload's Basic Auth
    # — the browser prompts once and both requests are covered.
    viewer = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard", "radar.html")
    if os.path.exists(viewer):
        upload_to_ftp(viewer, "radar.html")

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
