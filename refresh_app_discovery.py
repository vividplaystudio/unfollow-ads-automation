#!/usr/bin/env python3
"""App Discovery — read Apple's published list of new apps, then watch the ones
that gain traction.

DISCOVERY: APPLE PUBLISHES THE LIST. robots.txt on apps.apple.com declares a
"new-app" sitemap index — 400 gzipped files, ~140 unique apps each, ~55k total.
Sampling 200 of them gave a median age of 15 days with 98% under a month, so
"new" means what it says.

This replaced ID-space walking, which inferred at a 0.25% hit rate what Apple
hands over directly. The comparison was not close:

    ID walking   1,200 lookups  ->    ~160 apps   (~1/3 of daily launches)
    sitemaps       400 fetches  -> ~55,000 apps   (effectively complete)

Each app appears once per storefront (~7,000 URLs for ~140 apps), so dedupe on
the ID, not the URL.

THE COST ASYMMETRY THAT SHAPES THE REST. Discovery is now cheap; monitoring was
always cheap (400 IDs per lookup, ~2.4s each). The engine is the WATCHLIST:
record an app once, then track it twice daily for almost nothing. Only apps not
already tracked need a metadata lookup, so after the first run that half shrinks
to a small remainder.

⚠️ THE LOOKUP ENDPOINT IGNORES entity=software — see lookup_ids(). It returns
songs, albums and artist pages alongside apps, and songs carry a trackName too.

PRUNING IS THE FEATURE. Roughly a thousand apps launch daily and almost all die.
Thresholds sit at 60/90 days rather than 30/60 because a dev spends the first
month iterating — paywall, creative, hook — and cutting at 30 days killed apps
right before their ads started converting.

Outputs:
  app_watchlist.json   — full tracking state, script-only, several MB
  discovery.json       — top apps by score, sized for the browser
  discovery_state.json — velocity baseline (discovery itself is stateless now)
"""

import gzip
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone

# Shared helpers. The radar module has no import-time side effects — its
# top-level is env reads only, and main() is guarded — so this is safe.
from refresh_opportunity_radar import (
    AD_LIBRARY_URL, PREV_PASS, PREV_USER, get_json, upload_to_ftp,
)

WATCHLIST_OUTPUT = "app_watchlist.json"
DISCOVERY_OUTPUT = "discovery.json"
STATE_OUTPUT = "discovery_state.json"
BASE_URL = os.environ.get("RADAR_BASE_URL") or "https://genivox.com/ads-upload"

LOOKUP_BATCH = 400            # measured ceiling; 800 -> 502
SCAN_COUNTRY = (os.environ.get("DISCOVERY_COUNTRY") or "us").lower()
THROTTLE = float(os.environ.get("DISCOVERY_THROTTLE") or 0.35)

# Wall-clock budget for the whole scan. A 400-ID lookup costs ~2.4s of request
# time, so throughput is ~22 calls/min — call counts cannot predict runtime, a
# deadline can. An earlier build sized against the throttle alone and ran past
# the 90-minute job timeout, uploading nothing.
SCAN_MINUTES = float(os.environ.get("DISCOVERY_SCAN_MINUTES") or 25)

MAX_AGE_DAYS = int(os.environ.get("DISCOVERY_MAX_AGE_DAYS") or 180)
BROWSER_TOP_N = int(os.environ.get("DISCOVERY_BROWSER_TOP") or 1500)



def fetch_json(name: str, default):
    try:
        return get_json(f"{BASE_URL}/{name}", retries=1, auth=(PREV_USER, PREV_PASS))
    except Exception as e:  # noqa: BLE001 — absent on the first run
        print(f"  no {name} ({e})")
        return default


def lookup_ids(ids: list) -> tuple:
    """One batched lookup. Returns (apps, total_results).

    ⚠️ THE LOOKUP ENDPOINT IGNORES entity=software. Unlike /search, /lookup
    returns whatever the ID happens to be — songs, albums, artist pages,
    software, all mixed. A 40-ID probe came back as 7 tracks, 6 collections,
    3 artists and only 2 apps. Filtering on trackName alone (songs have one
    too) floods the watchlist with music: an early run surfaced "SIGNAL ANOMALY
    Phonk" and "Down in this Divebar" as top discoveries. kind == "software"
    is the only reliable discriminator.

    The total count is returned alongside for callers that need to tell "this
    ID resolved to something that isn't an app" from "this ID resolves to
    nothing at all".
    """
    url = ("https://itunes.apple.com/lookup?id=" + ",".join(map(str, ids))
           + f"&country={SCAN_COUNTRY}&entity=software")
    try:
        # retries=1 deliberately. Batched lookups return HTTP 500 often enough
        # (~12% on sparse input) that the default 3 retries add ~7s of backoff
        # per failure for nothing. A failed block is treated as empty; the next
        # run picks those IDs up again.
        res = get_json(url, retries=1).get("results", [])
    except Exception:  # noqa: BLE001 — a bad block must not stop the sweep
        return [], 0
    return [r for r in res if r.get("kind") == "software" and r.get("trackId")], len(res)


SITEMAP_INDEX = (os.environ.get("DISCOVERY_SITEMAP_INDEX")
                 or "https://apps.apple.com/sitemaps_apps_index_new-app_1.xml")
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def crawl_new_app_sitemaps(deadline: float = None) -> set:
    """Read Apple's PUBLISHED list of new apps instead of inferring it.

    Returns a set of app-ID strings. Each app appears once per storefront
    (~7,000 URLs for ~140 apps), so dedupe on the ID rather than the URL.

    A failed file is skipped rather than retried — with 400 of them, losing one
    costs ~140 apps that the next run picks up anyway, whereas retrying on a
    deadline-bound crawl costs coverage everywhere else.
    """
    print(f"  index: {SITEMAP_INDEX}")
    try:
        req = urllib.request.Request(SITEMAP_INDEX, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=90) as r:
            files = re.findall(r"<loc>(.*?)</loc>", r.read().decode("utf-8", "ignore"))
    except Exception as e:  # noqa: BLE001
        print(f"  ! sitemap index unreachable ({e})")
        return set()

    ids, done, failed = set(), 0, 0
    for f in files:
        if deadline and time.time() > deadline:
            print(f"  stopped at time budget after {done}/{len(files)} files")
            break
        try:
            req = urllib.request.Request(f, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                body = gzip.decompress(r.read()).decode("utf-8", "ignore")
            ids |= set(re.findall(r"/id(\d+)", body))
        except Exception:  # noqa: BLE001 — one bad file must not stop the crawl
            failed += 1
        done += 1
        if done % 50 == 0:
            print(f"  {done}/{len(files)} files · {len(ids):,} unique apps")
        time.sleep(THROTTLE)

    print(f"  {done} files read ({failed} failed) → {len(ids):,} unique app IDs")
    return ids




def refresh_watchlist(watch: dict, now) -> int:
    """Re-look-up everything already known. This is the cheap half — 400 per
    call means tens of thousands of apps cost seconds."""
    ids = list(watch.keys())
    updated = 0
    for i in range(0, len(ids), LOOKUP_BATCH):
        block, _ = lookup_ids(ids[i:i + LOOKUP_BATCH])
        for r in block:
            w = watch.get(str(r["trackId"]))
            if w:
                w["r"] = r.get("userRatingCount") or 0
                w["n"] = r.get("trackName") or w.get("n")
                updated += 1
        time.sleep(THROTTLE)
    return updated


def prune(watch: dict, now) -> int:
    """Drop the dead. ~1000 apps launch daily and almost all go nowhere; without
    this the watchlist fills with corpses and the signal drowns.

    Thresholds sit at 60/90 days rather than 30/60. A dev spends the first month
    iterating — fixing the paywall, re-cutting creative, finding the hook — so
    cutting at 30 days kills apps right before their ads start converting, which
    is exactly the moment worth catching. The cost is a watchlist that holds
    roughly twice as many apps, and a refresh pass that grows with it.
    """
    drop = []
    for aid, w in watch.items():
        age, r = w["age"], w.get("r", 0)
        if age > MAX_AGE_DAYS:
            drop.append(aid)
        elif age > 90 and r < 20:
            drop.append(aid)
        elif age > 60 and r < 5:
            drop.append(aid)
    for aid in drop:
        del watch[aid]
    return len(drop)


def score(w: dict) -> float:
    """0-100. Weighted toward EARLY traction: an app reaching real numbers in
    its first 30 days is being advertised and the ads are converting. Organic
    growth does not look like that."""
    age = max(w["age"], 1)
    r = w.get("r", 0)
    vel = w.get("d") or 0.0                 # ratings/day since baseline
    avg = r / age                           # lifetime average
    b = {}
    # Current velocity — the primary signal.
    b["velocity"] = round(min(vel, 40) / 40 * 40, 1)
    # Early traction — same rate is worth far more at day 12 than day 120.
    b["early"] = round((min(avg, 20) / 20 * 30) * (1 if age <= 30 else 0.4), 1)
    # Acceleration — climbing rather than already peaked. Needs a baseline.
    b["accel"] = round(min(vel / avg, 3) / 3 * 20, 1) if (vel and avg > 0.2) else 0.0
    # Youth — earlier catch, more runway to copy.
    b["youth"] = round(max(0, (90 - age) / 90) * 10, 1)
    w["score_breakdown"] = b
    return round(sum(b.values()), 1)


def main() -> None:
    now = datetime.now(timezone.utc)
    print(f"▶ App Discovery  {now.date()}  (budget {SCAN_MINUTES:.0f} min)")

    print("\n▶ Loading state")
    state = fetch_json(STATE_OUTPUT, {})
    watch = fetch_json(WATCHLIST_OUTPUT, {}).get("apps", {})
    print(f"  watchlist: {len(watch)} apps")

    deadline = time.time() + SCAN_MINUTES * 60

    # ── Discovery: read Apple's published new-app list ──
    print("\n▶ Reading Apple's new-app sitemaps")
    listed = crawl_new_app_sitemaps(deadline)

    # Only look up what isn't already tracked. After the first run this is a
    # small remainder, so the expensive half all but disappears.
    unknown = sorted(listed - set(watch))
    print(f"  {len(listed):,} listed · {len(listed) - len(unknown):,} already tracked "
          f"· {len(unknown):,} to look up")

    fresh = {}
    for i in range(0, len(unknown), LOOKUP_BATCH):
        if time.time() > deadline:
            print(f"  stopped at time budget ({len(fresh):,} resolved)")
            break
        block, _ = lookup_ids(unknown[i:i + LOOKUP_BATCH])
        for r in block:
            rel = r.get("releaseDate")
            if not rel:
                continue
            try:
                age = (now - datetime.fromisoformat(rel.replace("Z", "+00:00"))).days
            except ValueError:
                continue
            if 0 <= age <= MAX_AGE_DAYS:
                fresh[str(r["trackId"])] = r
        time.sleep(THROTTLE)
    print(f"  {len(fresh):,} resolved and within {MAX_AGE_DAYS}d")

    # ── Merge discoveries into the watchlist ──
    # Existing entries are never overwritten, so every app already tracked keeps
    # its first_seen, baseline and velocity history across this change.
    added = 0
    for aid, r in fresh.items():
        if aid in watch:
            continue
        rel = r["releaseDate"][:10]
        watch[aid] = {
            "n": r.get("trackName"), "p": r.get("sellerName") or r.get("artistName"),
            "g": r.get("primaryGenreName"), "rel": rel,
            "r": r.get("userRatingCount") or 0,
            "url": r.get("trackViewUrl"), "icon": r.get("artworkUrl100"),
            "price": r.get("formattedPrice"),
            "first_seen": now.date().isoformat(),
        }
        added += 1
    print(f"\n  {added} new apps added to the watchlist")

    # ── Refresh known apps, then compute ages/velocity ──
    print("\n▶ Refreshing known apps")
    prev_r = {aid: w.get("r", 0) for aid, w in watch.items()}
    updated = refresh_watchlist(watch, now)
    print(f"  {updated} refreshed")

    for aid, w in watch.items():
        try:
            w["age"] = (now - datetime.fromisoformat(w["rel"] + "T00:00:00+00:00")).days
        except ValueError:
            w["age"] = 999

    # Velocity against the stored baseline, rolled forward only after 12h so a
    # same-evening re-run cannot wipe out the comparison window.
    prev_state_at = state.get("baseline_at")
    span = None
    if prev_state_at:
        try:
            span = (now - datetime.fromisoformat(
                prev_state_at.replace("Z", "+00:00"))).total_seconds() / 86400
        except ValueError:
            span = None
    roll = span is None or span >= 0.5

    for aid, w in watch.items():
        base = w.get("b")
        if base is None:
            w["b"] = w.get("r", 0)
        elif span and span >= 0.25:
            delta = w.get("r", 0) - base
            w["d"] = round(delta / span, 1) if delta > 0 else 0.0
        # else: window too short to measure — leave the previous w["d"] intact
        # rather than blanking it. The watchlist dict is carried over from the
        # last run, so doing nothing here IS the carry-forward.
        if roll:
            w["b"] = w.get("r", 0)

    dropped = prune(watch, now)
    print(f"  pruned {dropped} dead apps  →  {len(watch)} tracked")

    for w in watch.values():
        w["score"] = score(w)

    ranked = sorted(watch.items(), key=lambda kv: -kv[1]["score"])

    # ── Write ──
    with open(WATCHLIST_OUTPUT, "w") as f:
        json.dump({"generated_at": now.isoformat(), "count": len(watch),
                   "apps": watch}, f, separators=(",", ":"), default=str)

    # Browser copy is capped — the full watchlist can reach several MB and no
    # page needs 90,000 rows.
    with open(DISCOVERY_OUTPUT, "w") as f:
        json.dump({
            "generated_at": now.isoformat(),
            "baseline_at": now.isoformat() if roll else prev_state_at,
            "span_days": round(span, 2) if span else None,
            "tracked": len(watch), "added_today": added,
            "listed_in_sitemaps": len(listed),
            "apps": [
                {**w, "app_id": aid, "ad_library_url": AD_LIBRARY_URL.format(
                    q=(w.get("n") or "").replace(" ", "+"))}
                for aid, w in ranked[:BROWSER_TOP_N]
            ],
        }, f, indent=2, default=str)

    with open(STATE_OUTPUT, "w") as f:
        # Discovery is STATELESS now. The sitemap index is re-read in full each
        # run, so there is no cursor to resume from and no top_id to corrupt —
        # which removes the class of bug that stored 7.93B, a value past the end
        # of the live ID range that would have poisoned every later run.
        json.dump({"generated_at": now.isoformat(),
                   "baseline_at": now.isoformat() if roll else prev_state_at,
                   "listed_in_sitemaps": len(listed)}, f, indent=2)

    print(f"\n✓ {len(watch)} tracked · {added} added · "
          f"{len(listed):,} listed in sitemaps · top {BROWSER_TOP_N} to browser")

    print("\n── Top 12 by discovery score ──")
    for aid, w in ranked[:12]:
        print(f"  {w['score']:>5}  {w.get('d', 0):>7}/day  {w['age']:>3}d  "
              f"{(w.get('r') or 0):>6} ratings  {(w.get('n') or '')[:34]:34} "
              f"{(w.get('g') or '')[:14]}")

    # JSON only — upload-dashboard.yml owns discovery.html. Uploading pages
    # from a long-running job races the dedicated uploader and the slower job
    # wins, silently reverting page fixes. See the note in
    # refresh_opportunity_radar.py.
    for fn in (WATCHLIST_OUTPUT, DISCOVERY_OUTPUT, STATE_OUTPUT):
        upload_to_ftp(fn, fn)


if __name__ == "__main__":
    main()
