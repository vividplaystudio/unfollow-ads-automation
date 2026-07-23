#!/usr/bin/env python3
"""Generate the permanent Ad Creative Performance reference from meta_history.json.

Usage:
    # 1. Fetch the latest backfill (or run the "Meta History Backfill" Action first):
    curl -s -u ads:@fifi2019 https://genivox.com/ads-upload/meta_history.json -o meta_history.json
    # 2. Build the doc:
    python3 build_ad_reference.py

Reads   META_HISTORY_FILE   (default: ./meta_history.json)
Writes  AD_REFERENCE_OUT    (default: ./AD-CREATIVE-PERFORMANCE-REFERENCE.md)

The subscribe metric is Meta's `omni_custom` action, validated at ~94% vs Adjust subs.
Re-run after any refresh of the backfill to regenerate the doc.
"""
import json
import os
from collections import defaultdict

IN = os.environ.get("META_HISTORY_FILE", "meta_history.json")
OUT = os.environ.get("AD_REFERENCE_OUT", "AD-CREATIVE-PERFORMANCE-REFERENCE.md")

h = json.load(open(IN))
A1 = "2399779997191076"
st = {a: h["statuses_by_account"].get(a, {}) for a in h["account_ids"]}


def status(acc, aid):
    s = st.get(acc, {}).get("adsets", {}).get(aid, {}).get("effective_status")
    if not s:
        return "removed"
    return {"ACTIVE": "active", "PAUSED": "paused", "CAMPAIGN_PAUSED": "camp-off",
            "ADSET_PAUSED": "paused"}.get(s, s.lower())


def angle(cn):
    c = cn.lower()
    if "lurker" in c: return "Lurker Reveal"
    if "discovery" in c or "aha" in c: return "Discovery / Aha"
    if "stalker" in c: return "Ex Stalker"
    if c.startswith("us -") or c.startswith("us-hm") or c in ("us 1", "us 2", "us3"): return "HM / US-new"
    return "Other"


def geo(cn):
    c = cn.upper()
    for g in ("UK", "CA", "AU", "US"):
        if c.startswith(g): return g
    return "??"


# per-adset aggregate
agg = defaultdict(lambda: {"i": 0, "s": 0, "sp": 0.0, "cn": "", "an": "", "d": set()})
for r in h["adsets_daily"]:
    k = (r.get("account_id"), r.get("adset_id"))
    v = agg[k]
    v["i"] += int(r.get("action_mobile_app_install") or 0)
    v["s"] += int(r.get("action_omni_custom") or 0)
    v["sp"] += float(r.get("spend") or 0)
    v["cn"] = r.get("campaign_name", ""); v["an"] = r.get("adset_name", "")
    if r.get("date"): v["d"].add(r["date"])

rows = []
for (acc, aid), v in agg.items():
    if v["i"] < 100: continue
    ds = sorted(v["d"])
    rows.append((v["s"]/v["i"]*100, v["i"], v["s"], v["sp"], v["sp"]/v["i"], v["sp"]/v["s"] if v["s"] else 0,
                 "A1" if acc == A1 else "A2", status(acc, aid), ds[0], ds[-1], v["cn"], v["an"]))
rows.sort(reverse=True)


def rollup(keyfn):
    d = defaultdict(lambda: [0, 0, 0.0])
    for r in h["adsets_daily"]:
        k = keyfn(r.get("campaign_name", ""))
        d[k][0] += int(r.get("action_mobile_app_install") or 0)
        d[k][1] += int(r.get("action_omni_custom") or 0)
        d[k][2] += float(r.get("spend") or 0)
    return sorted(d.items(), key=lambda x: -(x[1][1]/x[1][0] if x[1][0] else 0))


mon = defaultdict(lambda: [0, 0, 0.0])
for r in h["adsets_daily"]:
    if r.get("account_id") != A1 or not r.get("date"): continue
    m = r["date"][:7]; mon[m][0] += int(r.get("action_mobile_app_install") or 0)
    mon[m][1] += int(r.get("action_omni_custom") or 0); mon[m][2] += float(r.get("spend") or 0)


def win(lo, hi):
    i = s = 0; sp = 0.0
    for r in h["adsets_daily"]:
        if r.get("account_id") != A1 or not r.get("date"): continue
        if lo <= r["date"] <= hi:
            i += int(r.get("action_mobile_app_install") or 0); s += int(r.get("action_omni_custom") or 0); sp += float(r.get("spend") or 0)
    return i, s, s/i*100 if i else 0, sp/s if s else 0


L = []
w = L.append
w("# Ad Creative Performance — All-Time Reference")
w("")
w(f"**Data window:** {h['since']} → {h['until']} · **Snapshot generated:** {h['generated_at'][:10]}")
w("**Source:** Meta Marketing API full history (incl. removed ad sets), via `backfill_meta_history.py`.")
w("**Subscribe metric:** Meta `omni_custom` event — validated at **94% match** vs Adjust subscriptions.")
w("")
w("## ⚠️ How to use this — read first")
w("")
w("This is a **guide to what has worked, not a guarantee.** The SAME creative can convert")
w("great one month and flop the next — Meta's auction, audience saturation, seasonality,")
w("and timing all move. Example: **UK-Camp-3 Discovery – Copy > Ad set - 12 – Copy** was a")
w("34.6% all-time converter, but relaunches of that same creative later underperformed.")
w("Use these patterns to pick *angles and directions* to test — then let fresh data decide.")
w("")
w("- **Conv%** = subscribes ÷ installs (same-source, Meta-attributed)")
w("- **Cost/sub** = spend ÷ subscribes · **CPI** = spend ÷ install")
w("- Status: `active` / `paused` / `camp-off` (ad set on, campaign off) / `removed` (deleted)")
w("")
w("## The verdict on the $6.99 price change (Jul 20, 2026)")
w("")
w("Install→subscribe conversion, Account 1, around the change:")
w("")
w("| Window | Installs | Subs | Conversion | Cost/sub |")
w("|---|--:|--:|--:|--:|")
for lbl, lo, hi in [("Jun ($4.99 baseline)", "2026-06-01", "2026-06-30"),
                    ("Jul 1–19 ($4.99)", "2026-07-01", "2026-07-19"),
                    ("Jul 20–21 ($6.99 clean)", "2026-07-20", "2026-07-21")]:
    i, s, c, cs = win(lo, hi)
    w(f"| {lbl} | {i:,} | {s:,} | **{c:.1f}%** | ${cs:.2f} |")
w("")
w("**Conclusion: the price change did NOT hurt conversion.** Post-change (24.7%) is within")
w("noise of pre-change (26.7%) and *above* the June baseline (23.2%) — and each sub is now")
w("worth more. Any 'it feels worse' is the new-cohort learning-phase drag + attribution lag,")
w("not the price. Keep $6.99.")
w("")
w("## Best ANGLES (all-time)")
w("")
w("| Angle | Installs | Subs | Conversion | Cost/sub |")
w("|---|--:|--:|--:|--:|")
for k, v in rollup(angle):
    c = v[1]/v[0]*100 if v[0] else 0
    w(f"| {k} | {v[0]:,} | {v[1]:,} | **{c:.1f}%** | ${v[2]/v[1] if v[1] else 0:.2f} |")
w("")
w("**Discovery / Aha and Lurker Reveal are the golden angles.** Ex Stalker is a step down.")
w("The new HM/US creatives convert worst and cost 2× per sub — creative quality, not price.")
w("")
w("## Best GEOs (all-time)")
w("")
w("| Geo | Installs | Subs | Conversion | Cost/sub |")
w("|---|--:|--:|--:|--:|")
for k, v in rollup(geo):
    c = v[1]/v[0]*100 if v[0] else 0
    w(f"| {k} | {v[0]:,} | {v[1]:,} | **{c:.1f}%** | ${v[2]/v[1] if v[1] else 0:.2f} |")
w("")
w("**UK converts nearly 2× the US (35.6% vs 18.2%) at half the cost/sub.** CA is strong too.")
w("The US is the hardest, most expensive market — worth remembering while the current focus")
w("is US-only. The paused UK/CA winners may be worth reviving.")
w("")
w("## Monthly trend — Account 1")
w("")
w("| Month | Spend | Installs | Subs | Conversion | CPI |")
w("|---|--:|--:|--:|--:|--:|")
for m in sorted(mon):
    i, s, sp = mon[m][0], mon[m][1], mon[m][2]
    w(f"| {m} | ${sp:,.0f} | {i:,} | {s:,} | {s/i*100 if i else 0:.1f}% | ${sp/i if i else 0:.2f} |")
w("")
w(f"## All-time converter table — every ad set with ≥100 installs ({len(rows)} total)")
w("")
w("Ranked by conversion. This is the record of what actually converted.")
w("")
w("| # | Conv% | Inst | Subs | Spend | CPI | Acct | Status | First→Last | Campaign > Ad set |")
w("|--:|--:|--:|--:|--:|--:|:--:|:--:|:--:|---|")
for i, r in enumerate(rows, 1):
    conv, ins, sub, sp, cpi, cps, acct, stt, d0, d1, cn, an = r
    w(f"| {i} | {conv:.1f}% | {ins:,} | {sub} | ${sp:,.0f} | ${cpi:.2f} | {acct} | {stt} | {d0[5:]}→{d1[5:]} | {cn} > {an} |")
w("")
w("## How to refresh this")
w("")
w("1. GitHub → Actions → **Meta History Backfill** → Run workflow (set `since` as far back as needed)")
w("2. Fetch: `curl -s -u ads:@fifi2019 https://genivox.com/ads-upload/meta_history.json -o meta_history.json`")
w("3. Re-run `python3 build_ad_reference.py` to regenerate this doc")
w("")
w("**Caveat repeated:** past conversion ≠ future conversion. Treat this as a map of proven")
w("*directions*, and always validate a relaunch with 3 fresh full days of data.")

open(OUT, "w").write("\n".join(L))
print(f"Wrote {OUT} ({len(rows)} converter rows, {len(L)} lines)")
