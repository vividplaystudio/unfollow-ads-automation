#!/usr/bin/env python3
"""App Discovery — find every new app by walking Apple's ID space, then watch
the ones that show traction.

WHY ID WALKING RATHER THAN SEARCH. Keyword search only returns what you already
thought to search for, so it structurally cannot surface a niche nobody has
named. Counting IDs is unbiased: it finds whatever exists. A probe of twelve
consecutive IDs turned up "Alkebulan", "QUIERO CASARME CONTIGO" and
"BYTHEDØZEN" — none of which any keyword list would have reached.

THE COST ASYMMETRY THAT SHAPES THIS SCRIPT. Discovery is expensive; monitoring
is nearly free. Scanning the ~40M IDs covering 180 days takes ~11 hours, but
re-checking 90,000 apps you already know about takes ~90 seconds (400 IDs per
lookup call). So the engine is not the scan — it is the WATCHLIST. Discover an
app once, then track it daily for almost nothing.

MEASURED FACTS THIS RELIES ON (all verified against the live API):
  - Lookup accepts 400 IDs per call. 800 returns a 502.
  - Sequential scanning hits 25-66% live apps; RANDOM probing across the same
    range hits 3.7%. IDs are issued in dense clusters, so counting beats
    guessing by ~10x. Never sample randomly here.
  - Apple issues ~326k IDs/day (32M IDs spanned 98 days), across apps,
    developer accounts, music and books — not apps alone.
  - The live region is roughly the top 40M IDs. Below that it is mostly dead:
    7 of 11 sampled blocks between -40M and -400M were completely empty. Hence
    probe-and-skip rather than walking every number.
  - ID order tracks release date but NOT perfectly — ~1% of recent apps sit at
    far lower IDs (one 55-day-old app had ID 1.67B). Backward walking finds
    almost everything, not literally everything.

PRUNING IS THE FEATURE. Roughly a thousand apps launch daily and almost all
die. Without aggressive pruning the watchlist becomes a hundred thousand dead
listings and the signal drowns. Anything that hasn't shown traction by day 30
is dropped.

Outputs:
  app_watchlist.json — full tracking state, script-only, can be several MB
  discovery.json     — top apps by score, sized for the browser
  discovery_state.json — scan cursor so each run resumes where the last stopped
"""

import json
import os
import time
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

# Total lookup calls per run, split between frontier and backfill. 3000 calls
# x 400 IDs = 1.2M IDs, roughly 20 minutes at the default throttle.
SCAN_CALLS = int(os.environ.get("DISCOVERY_SCAN_CALLS") or 3000)
FRONTIER_CALLS = int(os.environ.get("DISCOVERY_FRONTIER_CALLS") or 900)

MAX_AGE_DAYS = int(os.environ.get("DISCOVERY_MAX_AGE_DAYS") or 180)
# Consecutive empty blocks before declaring a dead zone and jumping. 3 blocks
# = 1200 IDs of nothing, which does not happen inside a live cluster.
EMPTY_RUN_TO_SKIP = 3
SKIP_AHEAD = 2_000_000        # how far to jump past a dead zone
BROWSER_TOP_N = int(os.environ.get("DISCOVERY_BROWSER_TOP") or 1500)

# Seed used only when no state exists yet; the first frontier scan corrects it.
SEED_TOP_ID = int(os.environ.get("DISCOVERY_SEED_TOP_ID") or 6_794_400_000)


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

    The caller needs BOTH counts: apps are what we collect, but total decides
    whether a block is a genuine dead zone. Apps are only ~1% of IDs (0-14 per
    400) while all content types together run ~40%, so a block with zero apps
    is normal inside live space — judging emptiness on apps alone would skip
    straight over regions that do contain them.
    """
    url = ("https://itunes.apple.com/lookup?id=" + ",".join(map(str, ids))
           + f"&country={SCAN_COUNTRY}&entity=software")
    try:
        res = get_json(url).get("results", [])
    except Exception:  # noqa: BLE001 — a bad block must not stop the sweep
        return [], 0
    return [r for r in res if r.get("kind") == "software" and r.get("trackId")], len(res)


def scan_range(start: int, direction: int, calls: int, now, label: str) -> tuple:
    """Walk IDs in blocks, skipping dead zones. Returns (apps, calls_used, end_id).

    direction: +1 scans upward (frontier, newest), -1 downward (backfill).
    """
    found, used, cursor, empty_run = {}, 0, start, 0
    while used < calls:
        lo = cursor if direction > 0 else cursor - LOOKUP_BATCH
        block, total = lookup_ids(range(lo, lo + LOOKUP_BATCH))
        used += 1
        time.sleep(THROTTLE)

        # Liveness is judged on TOTAL results, not apps — see lookup_ids.
        if total:
            empty_run = 0
            for r in block:
                rel = r.get("releaseDate")
                if not rel:
                    continue
                try:
                    age = (now - datetime.fromisoformat(rel.replace("Z", "+00:00"))).days
                except ValueError:
                    continue
                # Raw ID space contains junk dates — 1979-01-01 placeholders and
                # future-dated pre-orders. Both must be rejected.
                if age < 0 or age > MAX_AGE_DAYS:
                    continue
                found[str(r["trackId"])] = r
        else:
            empty_run += 1
            if empty_run >= EMPTY_RUN_TO_SKIP:
                # Dead zone. Jumping costs one probe instead of thousands of
                # wasted calls walking empty space.
                cursor += direction * SKIP_AHEAD
                empty_run = 0
                continue

        cursor += direction * LOOKUP_BATCH

    print(f"  {label}: {used} calls, {used * LOOKUP_BATCH:,} IDs probed, "
          f"{len(found)} apps within {MAX_AGE_DAYS}d")
    return found, used, cursor


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
    this the watchlist fills with corpses and the signal drowns."""
    drop = []
    for aid, w in watch.items():
        age, r = w["age"], w.get("r", 0)
        if age > MAX_AGE_DAYS:
            drop.append(aid)
        elif age > 60 and r < 20:
            drop.append(aid)
        elif age > 30 and r < 5:
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
    print(f"▶ App Discovery  {now.date()}  (budget {SCAN_CALLS} calls)")

    print("\n▶ Loading state")
    state = fetch_json(STATE_OUTPUT, {})
    watch = fetch_json(WATCHLIST_OUTPUT, {}).get("apps", {})
    print(f"  watchlist: {len(watch)} apps")

    top_id = int(state.get("top_id") or SEED_TOP_ID)
    cursor = int(state.get("cursor") or top_id)
    floor = int(state.get("floor") or (top_id - 40_000_000))
    print(f"  top_id={top_id:,}  backfill cursor={cursor:,}  floor={floor:,}")
    done_pct = (top_id - cursor) / max(top_id - floor, 1) * 100
    print(f"  backfill {min(done_pct, 100):.1f}% complete")

    # ── Frontier: newest IDs first, so brand-new apps appear immediately
    # rather than waiting for the backfill to finish ──
    print("\n▶ Frontier scan (newest IDs)")
    fresh, used_f, new_top = scan_range(top_id, +1, FRONTIER_CALLS, now, "frontier")

    # ── Backfill: walk downward through the live region ──
    print("\n▶ Backfill scan (older IDs)")
    remaining = max(SCAN_CALLS - used_f, 0)
    older, used_b, new_cursor = ({}, 0, cursor)
    if remaining and cursor > floor:
        older, used_b, new_cursor = scan_range(cursor, -1, remaining, now, "backfill")
    elif cursor <= floor:
        print("  backfill complete — nothing older to cover")

    # ── Merge discoveries into the watchlist ──
    added = 0
    for aid, r in {**fresh, **older}.items():
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
        elif span:
            delta = w.get("r", 0) - base
            w["d"] = round(delta / max(span, 0.25), 1) if delta > 0 else 0.0
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
            "backfill_pct": round(min(done_pct, 100), 1),
            "apps": [
                {**w, "app_id": aid, "ad_library_url": AD_LIBRARY_URL.format(
                    q=(w.get("n") or "").replace(" ", "+"))}
                for aid, w in ranked[:BROWSER_TOP_N]
            ],
        }, f, indent=2, default=str)

    with open(STATE_OUTPUT, "w") as f:
        json.dump({"generated_at": now.isoformat(),
                   "baseline_at": now.isoformat() if roll else prev_state_at,
                   "top_id": max(new_top, top_id), "cursor": new_cursor,
                   "floor": floor,
                   "backfill_complete": new_cursor <= floor}, f, indent=2)

    print(f"\n✓ {len(watch)} tracked · {added} added · "
          f"backfill {min(done_pct, 100):.1f}% · top {BROWSER_TOP_N} to browser")

    print("\n── Top 12 by discovery score ──")
    for aid, w in ranked[:12]:
        print(f"  {w['score']:>5}  {w.get('d', 0):>7}/day  {w['age']:>3}d  "
              f"{(w.get('r') or 0):>6} ratings  {(w.get('n') or '')[:34]:34} "
              f"{(w.get('g') or '')[:14]}")

    for fn in (WATCHLIST_OUTPUT, DISCOVERY_OUTPUT, STATE_OUTPUT):
        upload_to_ftp(fn, fn)
    page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard", "discovery.html")
    if os.path.exists(page):
        upload_to_ftp(page, "discovery.html")


if __name__ == "__main__":
    main()
