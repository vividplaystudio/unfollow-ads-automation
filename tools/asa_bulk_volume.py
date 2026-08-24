#!/usr/bin/env python3
"""
Look up Apple's search volume for a list of keywords, one market at a time.

WHY THIS IS A SCRIPT AND NOT A BATCH CALL
-----------------------------------------
Apple's search-term-popularity endpoint takes a SINGULAR filter value -- a
"values" array is rejected as an unrecognized property -- so terms cannot be
batched. It is one request per term per country, which is fine for a few
hundred keywords but has to be a script rather than something typed by hand.

It also answers a question worth answering directly: does MobileAction's
volume estimate agree with Apple's own? The two are independent, and a keyword
plan built on the wrong one is expensive.

Usage:
    KEYWORD_FILE=data/keywords_us.csv COUNTRY=US python3 tools/asa_bulk_volume.py

Reads a CSV with a `keyword` column (any other columns are carried through),
prints a CSV to stdout with Apple's popularity, rank and genre appended.
"""

import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh_dashboard_json as legacy

KEYWORD_FILE = os.environ.get("KEYWORD_FILE", "data/keywords_us.csv")
COUNTRY = os.environ.get("COUNTRY", "US").strip().upper()
LIMIT = int(os.environ.get("LIMIT", "0"))


def main() -> int:
    if not legacy.ASA_V1_ENABLED:
        print("ASA credentials missing", file=sys.stderr)
        return 1
    try:
        legacy.asa_v1_token()
    except Exception as e:
        print(f"auth failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    with open(KEYWORD_FILE, encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if LIMIT:
        rows = rows[:LIMIT]
    terms = [r["keyword"].strip() for r in rows if r.get("keyword", "").strip()]
    print(f"looking up {len(terms)} terms in {COUNTRY}", file=sys.stderr)

    t0 = time.time()
    found = legacy.asa_v1_keyword_popularity(terms, country=COUNTRY)
    print(f"resolved {len(found)}/{len(terms)} in {time.time()-t0:.0f}s", file=sys.stderr)

    extra = ["apple_volume", "apple_rank", "apple_genre", "apple_month", "delta"]
    w = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()) + extra)
    w.writeheader()
    for r in rows:
        hit = found.get(r["keyword"].strip().lower()) or {}
        r["apple_volume"] = hit.get("popularity", "")
        r["apple_rank"] = hit.get("rank_in_genre", "")
        r["apple_genre"] = hit.get("genre", "")
        r["apple_month"] = hit.get("month", "")
        try:
            r["delta"] = int(hit["popularity"]) - int(r["ma_volume"])
        except (KeyError, TypeError, ValueError):
            r["delta"] = ""
        w.writerow(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
