"""
SQLite fact store for the ads dashboard.

WHY THIS EXISTS
---------------
The original pipeline re-derived every number from the source APIs on every
run: walk all RevenueCat customers, fetch attribution and subscription detail
for each, aggregate, publish. That makes the cost of a refresh proportional to
*total install history* rather than to *how much happened since last time*.

In May that was 15 minutes. By August, at ~110k customers, the customer walk
alone took 44 minutes and enrichment another 26 -- longer than the 30-minute
cron interval, so runs overlapped, raced on data.json, and the dashboard
silently froze on 2026-07-01 data.

The fix is not a faster walk. It is to stop walking. Three observations:

  1. Transactions are APPEND-ONLY. A payment that happened on Aug 3 will never
     change. The RC webhook receiver already captures every one of them into
     rc_events.jsonl (with .archive rotation), so the full history is on disk.

  2. Attribution is IMMUTABLE. $mediaSource / $campaign / $keyword / $adGroup
     are stamped at install and never change. Fetch once per customer, keep
     forever, never ask again.

  3. Ad spend is IMMUTABLE after a short restatement window. Apple and Meta
     can revise the last ~3 days; everything older is final. Re-fetch a
     trailing window, treat the rest as settled.

So the store holds facts, ingest appends only what is new, and the dashboard
is a set of aggregate queries. Refresh cost becomes proportional to activity,
not to history -- which is the property the old design lacked and the reason
it degraded a little more every day.

DESIGN NOTES
------------
* Every table is idempotent to write: re-ingesting the same webhook file or
  the same reporting day must not double-count. All writers use INSERT OR
  REPLACE / ON CONFLICT against a natural primary key, never bare INSERT.

* `day` columns are UTC 'YYYY-MM-DD' strings. Storing the bucket alongside the
  raw timestamp costs a few bytes and removes date arithmetic from every
  aggregate query, which is where the reporting windows get expensive.

* WAL mode: the cPanel cron may run the hot path while the nightly cold path
  is still going. WAL lets readers proceed during a write instead of throwing
  "database is locked" and losing a refresh.
"""

import os
import sqlite3
from datetime import datetime, timezone


# The store lives beside the scripts, NOT in the web-served dashboard folder.
# It contains per-customer attribution and would otherwise sit behind nothing
# but HTTP basic auth.
DEFAULT_STORE_PATH = os.environ.get(
    "STORE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "store.db"),
)


SCHEMA = """
-- ── Facts ────────────────────────────────────────────────────────────────

-- One row per transaction, sourced from the RC webhook log.
-- event_id is RevenueCat's own event id: replaying the same log is a no-op.
CREATE TABLE IF NOT EXISTS txn (
    event_id     TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL,
    ts_ms        INTEGER NOT NULL,
    day          TEXT NOT NULL,
    amount       REAL NOT NULL DEFAULT 0,   -- gross, USD (RC 'price')
    proceeds     REAL NOT NULL DEFAULT 0,   -- what actually lands: amount x (1 - VAT) x 0.85
    tax_pct      REAL,
    commission_pct REAL,                   -- RC's raw value; reports 30%, ignores Small Business Program
    expiration_ms INTEGER,                 -- lets active/cancelled be derived without any API call
    tier         TEXT,
    is_renewal   INTEGER NOT NULL DEFAULT 0,
    is_refund    INTEGER NOT NULL DEFAULT 0,
    product_id   TEXT,
    store        TEXT,
    country      TEXT,
    event_type   TEXT
);
CREATE INDEX IF NOT EXISTS ix_txn_day      ON txn(day);
CREATE INDEX IF NOT EXISTS ix_txn_customer ON txn(customer_id);

-- Immutable per-customer attribution. Written once, then left alone.
-- `channel` is the normalized media source the dashboard groups by; the raw
-- value is kept in media_source so a normalization change can be replayed
-- from the store without re-hitting the API.
CREATE TABLE IF NOT EXISTS customer (
    customer_id   TEXT PRIMARY KEY,
    first_seen_ms INTEGER,
    first_day     TEXT,
    country       TEXT,
    media_source  TEXT,
    channel       TEXT,
    campaign      TEXT,
    adgroup       TEXT,
    keyword       TEXT,
    attrs_json    TEXT,
    fetched_at_ms INTEGER,      -- row last touched
    -- Set ONLY when attribution has actually been resolved for this customer,
    -- whether or not the answer was 'no attribution'. NULL means 'never asked',
    -- and is what the cold path's backlog query selects on. Keeping this apart
    -- from fetched_at_ms is what makes the lookup once-per-customer-ever
    -- instead of once-per-run: the customer-list walk touches every row, and
    -- must not thereby claim their attribution is known.
    attrs_fetched_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_customer_first_day ON customer(first_day);
CREATE INDEX IF NOT EXISTS ix_customer_channel   ON customer(channel);
CREATE INDEX IF NOT EXISTS ix_customer_keyword   ON customer(campaign, keyword);

-- Mutable subscription state. Only meaningful for customers who ever paid,
-- so the refresher scopes itself to ids present in txn.
CREATE TABLE IF NOT EXISTS sub_state (
    customer_id   TEXT PRIMARY KEY,
    active        INTEGER NOT NULL DEFAULT 0,
    canceled      INTEGER NOT NULL DEFAULT 0,
    weekly        INTEGER NOT NULL DEFAULT 0,
    monthly       INTEGER NOT NULL DEFAULT 0,
    yearly        INTEGER NOT NULL DEFAULT 0,
    sub_count     INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER
);

-- Ad-side facts, one row per day per targeting unit. The composite primary
-- key is what makes a re-fetch of the restatement window an upsert instead
-- of a duplicate. Non-keyword sources write '' for keyword_id.
CREATE TABLE IF NOT EXISTS ad_daily (
    day           TEXT NOT NULL,
    source        TEXT NOT NULL,
    campaign_id   TEXT NOT NULL DEFAULT '',
    adgroup_id    TEXT NOT NULL DEFAULT '',
    keyword_id    TEXT NOT NULL DEFAULT '',
    country       TEXT NOT NULL DEFAULT '',
    spend         REAL    NOT NULL DEFAULT 0,
    impressions   INTEGER NOT NULL DEFAULT 0,
    taps          INTEGER NOT NULL DEFAULT 0,
    installs      INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER,
    PRIMARY KEY (day, source, campaign_id, adgroup_id, keyword_id, country)
);
CREATE INDEX IF NOT EXISTS ix_ad_daily_day ON ad_daily(day, source);

-- ── Dimensions ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS keyword_dim (
    keyword_id    TEXT PRIMARY KEY,
    campaign_id   TEXT,
    adgroup_id    TEXT,
    text          TEXT,
    match_type    TEXT,
    bid           REAL,
    popularity    INTEGER,          -- Apple's 1-100 volume score; NULL = unknown, never 0
    rank_in_genre INTEGER,
    genre         TEXT,
    popularity_month TEXT,
    status        TEXT,
    updated_at_ms INTEGER
);

-- Apple's market-wide search volume, independent of whether we bid on a term.
-- Unfiltered, the popularity endpoint returns the most-searched terms per
-- country and genre, which is keyword RESEARCH rather than reporting: what
-- people actually search, ranked, with a volume score. Kept per month because
-- Apple publishes it monthly and the trend matters as much as the level.
CREATE TABLE IF NOT EXISTS search_term_popularity (
    month         TEXT NOT NULL,
    country       TEXT NOT NULL,
    genre         TEXT NOT NULL,
    term          TEXT NOT NULL,
    rank_in_genre INTEGER,
    popularity    INTEGER,
    popularity_1to5 INTEGER,
    updated_at_ms INTEGER,
    PRIMARY KEY (month, country, genre, term)
);
CREATE INDEX IF NOT EXISTS ix_stp_lookup ON search_term_popularity(country, term);
CREATE INDEX IF NOT EXISTS ix_stp_rank   ON search_term_popularity(country, genre, rank_in_genre);

CREATE TABLE IF NOT EXISTS campaign_dim (
    campaign_id   TEXT PRIMARY KEY,
    source        TEXT,
    name          TEXT,
    country       TEXT,
    status        TEXT,
    daily_budget  REAL,
    -- Which App Store app this campaign promotes. One Apple Ads org can carry
    -- campaigns for several apps, and revenue here comes from a RevenueCat
    -- project that knows about exactly one of them -- so a campaign for
    -- another app would post spend against zero revenue and quietly drag the
    -- whole ASA ROAS down. Stored so it can be filtered on rather than guessed
    -- from campaign names.
    promoted_app_id TEXT,
    updated_at_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_campaign_app ON campaign_dim(promoted_app_id);

CREATE TABLE IF NOT EXISTS adgroup_dim (
    adgroup_id    TEXT PRIMARY KEY,
    campaign_id   TEXT,
    source        TEXT,
    name          TEXT,
    status        TEXT,
    daily_budget  REAL,
    updated_at_ms INTEGER
);

-- ── Bookkeeping ──────────────────────────────────────────────────────────
-- Watermarks and run stats. Keeping them in the DB rather than a sidecar file
-- means a restored/copied store carries its own position with it.
CREATE TABLE IF NOT EXISTS meta_kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def ms_to_day(ts_ms) -> str:
    """UTC 'YYYY-MM-DD' for a millisecond timestamp."""
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def open_store(path: str = None) -> sqlite3.Connection:
    """Open (creating if needed) the fact store and apply the schema.

    Safe to call on every run: every statement in SCHEMA is IF NOT EXISTS, so
    this doubles as the migration step for a fresh host.
    """
    path = path or DEFAULT_STORE_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    # WAL so the nightly cold path and the 15-minute hot path can overlap
    # without one of them dying on a locked database.
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL is the right durability tradeoff here: every fact is replayable
    # from the webhook log or the ad APIs, so a lost tail after a hard crash
    # costs one re-ingest, not data.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


# Columns added after a store may already exist in the field. CREATE TABLE IF
# NOT EXISTS does nothing to a table that is already there, so a new column in
# SCHEMA would be silently absent on every deployed store and the next upsert
# would fail with "table has no column named ...". Each entry here is applied
# with ALTER TABLE when missing.
#
# Append-only: never remove an entry, or a store that skipped that release
# stops migrating. SQLite ALTER TABLE ADD COLUMN is cheap and rewrites nothing.
MIGRATIONS = [
    ("txn", "proceeds", "REAL NOT NULL DEFAULT 0"),
    ("txn", "tax_pct", "REAL"),
    ("txn", "commission_pct", "REAL"),
    ("txn", "expiration_ms", "INTEGER"),
    ("txn", "event_type", "TEXT"),
    ("customer", "attrs_fetched_ms", "INTEGER"),
    ("keyword_dim", "rank_in_genre", "INTEGER"),
    ("keyword_dim", "genre", "TEXT"),
    ("keyword_dim", "popularity_month", "TEXT"),
    ("campaign_dim", "promoted_app_id", "TEXT"),
]


def _migrate(conn) -> None:
    applied = []
    for table, column, decl in MIGRATIONS:
        try:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        except Exception:
            continue                     # table not created yet; SCHEMA covers it
        if not cols or column in cols:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            applied.append(f"{table}.{column}")
        except Exception as e:
            print(f"  [store] migration {table}.{column} failed: {e}")
    if applied:
        print(f"  [store] migrated: {', '.join(applied)}")


# ── Watermarks ───────────────────────────────────────────────────────────

def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT v FROM meta_kv WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else default


def set_meta(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta_kv (k, v) VALUES (?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, str(value)),
    )


def get_meta_int(conn, key: str, default: int = 0) -> int:
    try:
        return int(get_meta(conn, key, default))
    except (TypeError, ValueError):
        return default


# ── Writers ──────────────────────────────────────────────────────────────
# All of these are idempotent by primary key. Re-running ingest over the same
# input must produce the same store, because the webhook log is replayed from
# a watermark that can legitimately overlap.

def upsert_txns(conn, rows) -> int:
    """rows: iterable of dicts. Returns number of rows written."""
    payload = [
        (
            r["event_id"], r["customer_id"], int(r["ts_ms"]),
            r.get("day") or ms_to_day(r["ts_ms"]),
            float(r.get("amount") or 0), float(r.get("proceeds") or 0),
            r.get("tax_pct"), r.get("commission_pct"), r.get("expiration_ms"),
            r.get("tier"),
            1 if r.get("is_renewal") else 0,
            1 if r.get("is_refund") else 0,
            r.get("product_id"), r.get("store"), r.get("country"),
            r.get("event_type"),
        )
        for r in rows
    ]
    if not payload:
        return 0
    conn.executemany(
        "INSERT INTO txn (event_id, customer_id, ts_ms, day, amount, proceeds, "
        "                 tax_pct, commission_pct, expiration_ms, tier, "
        "                 is_renewal, is_refund, product_id, store, country, event_type) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(event_id) DO UPDATE SET "
        "  amount=excluded.amount, proceeds=excluded.proceeds, "
        "  tax_pct=excluded.tax_pct, commission_pct=excluded.commission_pct, "
        "  expiration_ms=MAX(COALESCE(excluded.expiration_ms,0), COALESCE(txn.expiration_ms,0)), "
        "  tier=excluded.tier, "
        "  is_renewal=excluded.is_renewal, is_refund=excluded.is_refund, "
        "  country=COALESCE(excluded.country, txn.country)",
        payload,
    )
    return len(payload)


def upsert_customers(conn, rows, attribution_resolved: bool = False) -> int:
    """Write customer rows.

    Uses COALESCE on update so a later fetch that returns a blank field cannot
    erase a value we already captured -- RC occasionally returns partial
    attribute sets, and a blank there is 'unknown', not 'none'.

    attribution_resolved=True marks these customers as "we have asked about
    attribution and this is the answer", which permanently removes them from
    the cold path's lookup backlog. Pass it only from a caller that actually
    resolved attribution -- the plain customer-list walk must not, or every
    customer would be marked known after the first night and never enriched.
    """
    now = utc_now_ms()
    stamp = now if attribution_resolved else None
    payload = [
        (
            r["customer_id"], r.get("first_seen_ms"),
            r.get("first_day") or (ms_to_day(r["first_seen_ms"]) if r.get("first_seen_ms") else None),
            r.get("country"), r.get("media_source"), r.get("channel"),
            r.get("campaign"), r.get("adgroup"), r.get("keyword"),
            r.get("attrs_json"), now, stamp,
        )
        for r in rows
    ]
    if not payload:
        return 0
    conn.executemany(
        "INSERT INTO customer (customer_id, first_seen_ms, first_day, country, "
        "                      media_source, channel, campaign, adgroup, keyword, "
        "                      attrs_json, fetched_at_ms, attrs_fetched_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(customer_id) DO UPDATE SET "
        "  first_seen_ms=COALESCE(excluded.first_seen_ms, customer.first_seen_ms), "
        "  first_day    =COALESCE(excluded.first_day,     customer.first_day), "
        "  country      =COALESCE(NULLIF(excluded.country,''),      customer.country), "
        "  media_source =COALESCE(NULLIF(excluded.media_source,''), customer.media_source), "
        "  channel      =COALESCE(NULLIF(excluded.channel,''),      customer.channel), "
        "  campaign     =COALESCE(NULLIF(excluded.campaign,''),     customer.campaign), "
        "  adgroup      =COALESCE(NULLIF(excluded.adgroup,''),      customer.adgroup), "
        "  keyword      =COALESCE(NULLIF(excluded.keyword,''),      customer.keyword), "
        "  attrs_json   =COALESCE(excluded.attrs_json, customer.attrs_json), "
        "  fetched_at_ms=excluded.fetched_at_ms, "
        "  attrs_fetched_ms=COALESCE(excluded.attrs_fetched_ms, customer.attrs_fetched_ms)",
        payload,
    )
    return len(payload)


def upsert_sub_state(conn, rows) -> int:
    now = utc_now_ms()
    payload = [
        (
            r["customer_id"],
            1 if r.get("active") else 0, 1 if r.get("canceled") else 0,
            int(r.get("weekly") or 0), int(r.get("monthly") or 0),
            int(r.get("yearly") or 0), int(r.get("sub_count") or 0), now,
        )
        for r in rows
    ]
    if not payload:
        return 0
    conn.executemany(
        "INSERT INTO sub_state (customer_id, active, canceled, weekly, monthly, "
        "                       yearly, sub_count, updated_at_ms) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(customer_id) DO UPDATE SET "
        "  active=excluded.active, canceled=excluded.canceled, "
        "  weekly=excluded.weekly, monthly=excluded.monthly, yearly=excluded.yearly, "
        "  sub_count=excluded.sub_count, updated_at_ms=excluded.updated_at_ms",
        payload,
    )
    return len(payload)


def upsert_ad_daily(conn, rows) -> int:
    """Ad facts for one day/targeting unit.

    Deliberately REPLACES the metrics rather than adding to them: the caller
    re-fetches whole days from the ad API, so the fetched value is the truth
    for that day. Adding would double-count on every restatement pass.
    """
    now = utc_now_ms()
    payload = [
        (
            r["day"], r["source"], r.get("campaign_id") or "",
            r.get("adgroup_id") or "", r.get("keyword_id") or "",
            r.get("country") or "",
            float(r.get("spend") or 0), int(r.get("impressions") or 0),
            int(r.get("taps") or 0), int(r.get("installs") or 0), now,
        )
        for r in rows
    ]
    if not payload:
        return 0
    conn.executemany(
        "INSERT INTO ad_daily (day, source, campaign_id, adgroup_id, keyword_id, "
        "                      country, spend, impressions, taps, installs, updated_at_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(day, source, campaign_id, adgroup_id, keyword_id, country) "
        "DO UPDATE SET spend=excluded.spend, impressions=excluded.impressions, "
        "              taps=excluded.taps, installs=excluded.installs, "
        "              updated_at_ms=excluded.updated_at_ms",
        payload,
    )
    return len(payload)


def upsert_keyword_dim(conn, rows) -> int:
    """Keyword metadata.

    popularity uses COALESCE on update: Apple returns it from a separate
    endpoint that can fail independently, and a failed lookup must not wipe
    the last known value. NULL means 'not known', never 'zero'.
    """
    now = utc_now_ms()
    payload = [
        (
            str(r["keyword_id"]), r.get("campaign_id"), r.get("adgroup_id"),
            r.get("text"), r.get("match_type"), r.get("bid"),
            r.get("popularity"), r.get("rank_in_genre"), r.get("genre"),
            r.get("popularity_month"), r.get("status"), now,
        )
        for r in rows
    ]
    if not payload:
        return 0
    conn.executemany(
        "INSERT INTO keyword_dim (keyword_id, campaign_id, adgroup_id, text, "
        "                         match_type, bid, popularity, rank_in_genre, "
        "                         genre, popularity_month, status, updated_at_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(keyword_id) DO UPDATE SET "
        "  campaign_id=COALESCE(excluded.campaign_id, keyword_dim.campaign_id), "
        "  adgroup_id =COALESCE(excluded.adgroup_id,  keyword_dim.adgroup_id), "
        "  text       =COALESCE(excluded.text,        keyword_dim.text), "
        "  match_type =COALESCE(excluded.match_type,  keyword_dim.match_type), "
        "  bid        =COALESCE(excluded.bid,         keyword_dim.bid), "
        "  popularity =COALESCE(excluded.popularity,  keyword_dim.popularity), "
        "  rank_in_genre=COALESCE(excluded.rank_in_genre, keyword_dim.rank_in_genre), "
        "  genre      =COALESCE(excluded.genre,       keyword_dim.genre), "
        "  popularity_month=COALESCE(excluded.popularity_month, keyword_dim.popularity_month), "
        "  status     =COALESCE(excluded.status,      keyword_dim.status), "
        "  updated_at_ms=excluded.updated_at_ms",
        payload,
    )
    return len(payload)


def upsert_campaign_dim(conn, rows) -> int:
    now = utc_now_ms()
    payload = [
        (str(r["campaign_id"]), r.get("source"), r.get("name"), r.get("country"),
         r.get("status"), r.get("daily_budget"), r.get("promoted_app_id"), now)
        for r in rows
    ]
    if not payload:
        return 0
    conn.executemany(
        "INSERT INTO campaign_dim (campaign_id, source, name, country, status, "
        "                          daily_budget, promoted_app_id, updated_at_ms) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(campaign_id) DO UPDATE SET "
        "  source=COALESCE(excluded.source, campaign_dim.source), "
        "  name  =COALESCE(excluded.name,   campaign_dim.name), "
        "  country=COALESCE(excluded.country, campaign_dim.country), "
        "  status=COALESCE(excluded.status, campaign_dim.status), "
        "  daily_budget=COALESCE(excluded.daily_budget, campaign_dim.daily_budget), "
        "  promoted_app_id=COALESCE(excluded.promoted_app_id, campaign_dim.promoted_app_id), "
        "  updated_at_ms=excluded.updated_at_ms",
        payload,
    )
    return len(payload)


def upsert_adgroup_dim(conn, rows) -> int:
    now = utc_now_ms()
    payload = [
        (str(r["adgroup_id"]), r.get("campaign_id"), r.get("source"), r.get("name"),
         r.get("status"), r.get("daily_budget"), now)
        for r in rows
    ]
    if not payload:
        return 0
    conn.executemany(
        "INSERT INTO adgroup_dim (adgroup_id, campaign_id, source, name, status, "
        "                         daily_budget, updated_at_ms) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(adgroup_id) DO UPDATE SET "
        "  campaign_id=COALESCE(excluded.campaign_id, adgroup_dim.campaign_id), "
        "  source=COALESCE(excluded.source, adgroup_dim.source), "
        "  name  =COALESCE(excluded.name,   adgroup_dim.name), "
        "  status=COALESCE(excluded.status, adgroup_dim.status), "
        "  daily_budget=COALESCE(excluded.daily_budget, adgroup_dim.daily_budget), "
        "  updated_at_ms=excluded.updated_at_ms",
        payload,
    )
    return len(payload)


# ── Queries used by ingest ───────────────────────────────────────────────

def upsert_search_term_popularity(conn, rows) -> int:
    now = utc_now_ms()
    payload = [
        (r["month"], r["country"], r.get("genre") or "", r["term"],
         r.get("rank_in_genre"), r.get("popularity"), r.get("popularity_1to5"), now)
        for r in rows if r.get("month") and r.get("country") and r.get("term")
    ]
    if not payload:
        return 0
    conn.executemany(
        "INSERT INTO search_term_popularity (month, country, genre, term, "
        "                                    rank_in_genre, popularity, "
        "                                    popularity_1to5, updated_at_ms) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(month, country, genre, term) DO UPDATE SET "
        "  rank_in_genre=excluded.rank_in_genre, popularity=excluded.popularity, "
        "  popularity_1to5=excluded.popularity_1to5, "
        "  updated_at_ms=excluded.updated_at_ms",
        payload,
    )
    return len(payload)


def customers_missing_attribution(conn, limit: int = None):
    """Payers we have transactions for but no attribution row yet.

    This is the query that replaces the 110k-customer walk: it returns only
    the handful of new payers since the last run.
    """
    sql = (
        "SELECT DISTINCT t.customer_id FROM txn t "
        "LEFT JOIN customer c ON c.customer_id = t.customer_id "
        "WHERE c.customer_id IS NULL"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [r["customer_id"] for r in conn.execute(sql)]


def paying_customers(conn, since_day: str = None):
    """Every customer with at least one transaction, optionally since a day."""
    if since_day:
        rows = conn.execute(
            "SELECT DISTINCT customer_id FROM txn WHERE day >= ?", (since_day,)
        )
    else:
        rows = conn.execute("SELECT DISTINCT customer_id FROM txn")
    return [r["customer_id"] for r in rows]


def checkpoint_and_close(conn) -> None:
    """Fold the WAL back into the main .db file, then close.

    Required before copying or uploading the store: in WAL mode the most
    recent writes live in a sibling -wal file, so shipping store.db alone
    would silently deliver a database missing everything written since the
    last automatic checkpoint.
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    conn.commit()
    conn.close()


def store_stats(conn) -> dict:
    def one(sql):
        r = conn.execute(sql).fetchone()
        return r[0] if r else 0
    return {
        "txn": one("SELECT COUNT(*) FROM txn"),
        "txn_first_day": one("SELECT MIN(day) FROM txn"),
        "txn_last_day": one("SELECT MAX(day) FROM txn"),
        "txn_gross": one("SELECT ROUND(SUM(amount),2) FROM txn"),
        "txn_proceeds": one("SELECT ROUND(SUM(proceeds),2) FROM txn"),
        "customers": one("SELECT COUNT(*) FROM customer"),
        "customers_attributed": one("SELECT COUNT(*) FROM customer WHERE media_source IS NOT NULL AND media_source <> ''"),
        "customers_attr_unknown": one("SELECT COUNT(*) FROM customer WHERE attrs_fetched_ms IS NULL"),
        "sub_state": one("SELECT COUNT(*) FROM sub_state"),
        "ad_daily": one("SELECT COUNT(*) FROM ad_daily"),
        "ad_daily_last": one("SELECT MAX(day) FROM ad_daily"),
        "keywords": one("SELECT COUNT(*) FROM keyword_dim"),
        "keywords_with_popularity": one("SELECT COUNT(*) FROM keyword_dim WHERE popularity IS NOT NULL"),
        "search_terms_ranked": one("SELECT COUNT(*) FROM search_term_popularity"),
    }
