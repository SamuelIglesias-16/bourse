"""Postgres-aware background scrape tasks for the API."""

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Generator

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from bourse.models import Listing
import bourse.plick as plick
import bourse.vinted as vinted

try:
    import bourse.tradera as tradera
    _tradera_available = True
except Exception:
    tradera = None  # type: ignore[assignment]
    _tradera_available = False
    logging.getLogger(__name__).warning("tradera module unavailable — will be skipped")

try:
    import bourse.blocket as blocket
    _blocket_available = True
except Exception:
    blocket = None  # type: ignore[assignment]
    _blocket_available = False
    logging.getLogger(__name__).warning("blocket module unavailable — will be skipped")

try:
    import bourse.ebay as ebay
    _ebay_available = True
except Exception:
    ebay = None  # type: ignore[assignment]
    _ebay_available = False
    logging.getLogger(__name__).warning("ebay module unavailable — will be skipped")

try:
    from bourse.alerts import check_alerts as _check_alerts
except Exception:
    _check_alerts = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning("alerts module unavailable — check_alerts disabled")

load_dotenv()  # no-op on Railway; picks up .env locally
logger = logging.getLogger(__name__)
WATCHLIST_FILE = Path("watchlist.txt")
_CONNECT_TIMEOUT = 10  # seconds before psycopg2.connect() gives up

_pg_failure_count = 0
_pg_backoff_until: datetime | None = None
_PG_BACKOFF_THRESHOLD = 3
_PG_BACKOFF_MINUTES = 10


# ── DB helpers ───────────────────────────────────────────────────────────────

@contextmanager
def pg_read() -> Generator[psycopg2.extensions.connection, None, None]:  # type: ignore[name-defined]
    conn = psycopg2.connect(
        os.environ["DATABASE_URL"],
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=_CONNECT_TIMEOUT,
    )
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def pg_write() -> Generator[psycopg2.extensions.connection, None, None]:  # type: ignore[name-defined]
    conn = psycopg2.connect(
        os.environ["DATABASE_URL"],
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=_CONNECT_TIMEOUT,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _upsert_listing(cur: Any, listing: Listing) -> None:
    import json as _json
    raw_blob = _json.dumps(listing.raw_extras) if listing.raw_extras else "{}"
    initial_status = listing.status_override or "active"
    cur.execute(
        """INSERT INTO listings
               (listing_id, platform, url, title, brand, size, condition, material,
                seller_name, seller_rating, posted_at, first_seen, last_seen,
                status, raw, image_url, shipping_sek)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (listing_id) DO UPDATE
               SET last_seen     = EXCLUDED.last_seen,
                   condition     = COALESCE(EXCLUDED.condition, listings.condition),
                   seller_rating = COALESCE(EXCLUDED.seller_rating, listings.seller_rating),
                   image_url     = COALESCE(EXCLUDED.image_url, listings.image_url),
                   shipping_sek  = COALESCE(EXCLUDED.shipping_sek, listings.shipping_sek),
                   status        = CASE WHEN %s IS NOT NULL THEN %s ELSE listings.status END""",
        (listing.listing_id, listing.platform, listing.url, listing.title,
         listing.brand, listing.size, listing.condition, listing.material,
         listing.seller_name, listing.seller_rating, listing.posted_at,
         listing.scraped_at, listing.scraped_at, initial_status, raw_blob,
         listing.image_url, listing.shipping_sek,
         listing.status_override, listing.status_override),
    )


def _insert_snapshot(cur: Any, listing: Listing) -> None:
    cur.execute(
        """INSERT INTO listing_snapshots
               (listing_id, scraped_at, price_sek, likes, views, position_in_search)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (listing.listing_id, listing.scraped_at, listing.price_sek,
         listing.likes, listing.views, listing.position_in_search),
    )


def _read_watchlist() -> list[str]:
    if not WATCHLIST_FILE.exists():
        return []
    return [
        line.strip()
        for line in WATCHLIST_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _write_to_pg(listings: list[Listing]) -> None:
    try:
        with pg_write() as conn:
            with conn.cursor() as cur:
                for l in listings:
                    _upsert_listing(cur, l)
                    _insert_snapshot(cur, l)
    except psycopg2.OperationalError as exc:
        logger.warning(
            "Postgres unreachable (%s) — falling back to SQLite for %d listings",
            exc, len(listings),
        )
        from bourse.ingest import ingest
        ingest(listings, query="")


# ── Watchlist Postgres store ──────────────────────────────────────────────────

def ensure_watchlist_table() -> None:
    """Create watchlist table in Postgres; seed from watchlist.txt on first run."""
    with pg_write() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS watchlist (
                    id    SERIAL PRIMARY KEY,
                    query TEXT UNIQUE NOT NULL
                )
            """)
            cur.execute("SELECT COUNT(*) AS n FROM watchlist")
            if cur.fetchone()["n"] == 0 and WATCHLIST_FILE.exists():
                lines = [l.strip() for l in WATCHLIST_FILE.read_text(encoding="utf-8").splitlines()
                         if l.strip() and not l.startswith("#")]
                for q in lines:
                    cur.execute("INSERT INTO watchlist (query) VALUES (%s) ON CONFLICT DO NOTHING", (q,))
                if lines:
                    logger.info("Seeded %d watchlist queries from watchlist.txt", len(lines))


def ensure_listing_columns() -> None:
    """Idempotent migration: add columns added after the original schema."""
    with pg_write() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS image_url TEXT")
            cur.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS shipping_sek INTEGER")


def ensure_opportunities_table() -> None:
    """Create arbitrage_opportunities table holding the latest signal per watchlist query."""
    with pg_write() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS arbitrage_opportunities (
                    query            TEXT PRIMARY KEY,
                    buy_platform     TEXT NOT NULL,
                    sell_platform    TEXT NOT NULL,
                    buy_median_sek   INTEGER NOT NULL,
                    sell_median_sek  INTEGER NOT NULL,
                    est_margin_sek   INTEGER NOT NULL,
                    est_margin_pct   REAL,
                    gross_margin_sek INTEGER NOT NULL,
                    total_listings   INTEGER NOT NULL,
                    computed_at      TIMESTAMP NOT NULL
                )
            """)


# ── Arbitrage opportunities computation ──────────────────────────────────────


def _platform_aggregates_for_query(cur: Any, query: str) -> list[dict[str, Any]]:
    """Per-platform aggregates (active + sold) for one watchlist query.

    Mirrors the keyword-match rule used by /platforms/compare (AND for ≥3
    words, OR for 1–2). Returns a list of dicts with:
      ``platform``, ``count``, ``median_price``, ``sold_count``, ``median_sold_price``.
    """
    from bourse.ingest import PRICE_MIN, PRICE_MAX  # local import to avoid cycle

    keywords = [kw for kw in query.split() if kw]
    if not keywords:
        return []

    joiner = " AND " if len(keywords) >= 3 else " OR "
    clauses = joiner.join(
        "(l.title ILIKE %s OR COALESCE(l.brand, '') ILIKE %s)" for _ in keywords
    )
    params: list[Any] = []
    for kw in keywords:
        pat = f"%{kw}%"
        params.extend([pat, pat])

    sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (listing_id) listing_id, price_sek
              FROM listing_snapshots ORDER BY listing_id, scraped_at DESC
        )
        SELECT
            l.platform,
            COUNT(*) FILTER (WHERE l.status = 'active') AS count,
            (PERCENTILE_CONT(0.5) WITHIN GROUP (
                ORDER BY CASE WHEN l.status = 'active' THEN lt.price_sek END
            ))::INTEGER AS median_price,
            COUNT(*) FILTER (
                WHERE l.status = 'sold' AND l.last_seen >= NOW() - INTERVAL '90 days'
            ) AS sold_count,
            (PERCENTILE_CONT(0.5) WITHIN GROUP (
                ORDER BY CASE WHEN l.status = 'sold' AND l.last_seen >= NOW() - INTERVAL '90 days'
                              THEN lt.price_sek END
            ))::INTEGER AS median_sold_price
          FROM listings l
          JOIN latest lt ON lt.listing_id = l.listing_id
         WHERE l.status IN ('active', 'sold')
           AND lt.price_sek BETWEEN {PRICE_MIN} AND {PRICE_MAX}
           AND ({clauses})
         GROUP BY l.platform
    """
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def backfill_vinted_sold() -> dict[str, int]:
    """Reclassify already-'removed' Vinted listings as 'sold' with estimated price.

    Vinted items rarely get deleted for moderation reasons — the vast majority
    of disappearances are sales. Before this function existed, our update flow
    couldn't distinguish 'sold' from 'removed' for Vinted, so we blanket-marked
    them 'removed'. This one-shot reclassification:

    1. Finds every Vinted listing with status='removed'
    2. Flips status to 'sold'
    3. Synthesizes a final snapshot priced at ``last_price × 0.9`` (10% discount
       — Vinted negotiation is typical) so the row appears in /listings?status=sold

    Safe to re-run; only inserts an estimated snapshot if none has already been
    written at-or-after the listing's last_seen timestamp.
    """
    try:
        with pg_write() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT l.listing_id, l.last_seen,
                              (SELECT price_sek FROM listing_snapshots
                                WHERE listing_id = l.listing_id
                                ORDER BY scraped_at DESC LIMIT 1) AS last_price
                         FROM listings l
                        WHERE l.platform = 'vinted' AND l.status = 'removed'"""
                )
                rows = list(cur.fetchall())

                reclassified = 0
                snapshots_added = 0
                skipped_no_price = 0

                for r in rows:
                    listing_id = r["listing_id"]
                    last_seen = r["last_seen"]
                    last_price = r["last_price"]

                    cur.execute(
                        "UPDATE listings SET status = 'sold' WHERE listing_id = %s",
                        (listing_id,),
                    )
                    reclassified += 1

                    if last_price is None:
                        skipped_no_price += 1
                        continue

                    estimated = int(round(last_price * (1 - SOLD_PRICE_ESTIMATE_DISCOUNT)))

                    cur.execute(
                        """SELECT COUNT(*) AS n FROM listing_snapshots
                            WHERE listing_id = %s AND scraped_at >= %s""",
                        (listing_id, last_seen),
                    )
                    already = cur.fetchone()["n"]
                    if already == 0:
                        cur.execute(
                            """INSERT INTO listing_snapshots
                                   (listing_id, scraped_at, price_sek, likes, views, position_in_search)
                                VALUES (%s,%s,%s,NULL,NULL,NULL)""",
                            (listing_id, last_seen, estimated),
                        )
                        snapshots_added += 1

        logger.info(
            "backfill_vinted_sold: reclassified=%d snapshots_added=%d skipped_no_price=%d",
            reclassified, snapshots_added, skipped_no_price,
        )
        return {
            "reclassified": reclassified,
            "snapshots_added": snapshots_added,
            "skipped_no_price": skipped_no_price,
        }
    except Exception:
        logger.exception("backfill_vinted_sold failed")
        return {"reclassified": 0, "snapshots_added": 0, "skipped_no_price": 0}


def refresh_opportunities() -> dict[str, int]:
    """Recompute the arbitrage signal for every watchlist query and persist.

    Queries with no eligible signal (fewer than 2 platforms with sample ≥5)
    are removed from the table. Safe to call after every scrape — runs ~30
    small queries against Postgres, no scraping.
    """
    from bourse.compare import compute_arbitrage_signal  # avoid import cycle

    queries = pg_read_watchlist()
    if not queries:
        return {"computed": 0, "removed": 0, "skipped": 0}

    try:
        ensure_opportunities_table()
    except Exception:
        logger.exception("refresh_opportunities: could not ensure table")
        return {"computed": 0, "removed": 0, "skipped": len(queries)}

    now = datetime.now()
    computed = removed = skipped = 0

    try:
        with pg_write() as conn:
            with conn.cursor() as cur:
                for query in queries:
                    try:
                        rows = _platform_aggregates_for_query(cur, query)
                    except Exception:
                        logger.exception("refresh_opportunities: aggregate failed for %r", query)
                        skipped += 1
                        continue

                    platforms = [
                        {
                            "platform": r["platform"],
                            "count": int(r["count"] or 0),
                            "median_price": int(r["median_price"]) if r.get("median_price") is not None else None,
                            "sold_count": int(r["sold_count"] or 0),
                            "median_sold_price": int(r["median_sold_price"]) if r.get("median_sold_price") is not None else None,
                        }
                        for r in rows
                    ]
                    sig = compute_arbitrage_signal(platforms)
                    if sig is None:
                        cur.execute("DELETE FROM arbitrage_opportunities WHERE query = %s", (query,))
                        if cur.rowcount > 0:
                            removed += 1
                        continue

                    total_listings = sum(p["count"] + p["sold_count"] for p in platforms)
                    cur.execute(
                        """INSERT INTO arbitrage_opportunities
                               (query, buy_platform, sell_platform,
                                buy_median_sek, sell_median_sek,
                                est_margin_sek, est_margin_pct, gross_margin_sek,
                                total_listings, computed_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (query) DO UPDATE SET
                               buy_platform     = EXCLUDED.buy_platform,
                               sell_platform    = EXCLUDED.sell_platform,
                               buy_median_sek   = EXCLUDED.buy_median_sek,
                               sell_median_sek  = EXCLUDED.sell_median_sek,
                               est_margin_sek   = EXCLUDED.est_margin_sek,
                               est_margin_pct   = EXCLUDED.est_margin_pct,
                               gross_margin_sek = EXCLUDED.gross_margin_sek,
                               total_listings   = EXCLUDED.total_listings,
                               computed_at      = EXCLUDED.computed_at""",
                        (
                            query,
                            sig["buy_platform"],
                            sig["sell_platform"],
                            sig["buy_median_sek"],
                            sig["sell_median_sek"],
                            sig["est_margin_sek"],
                            sig["est_margin_pct"],
                            sig["gross_margin_sek"],
                            total_listings,
                            now,
                        ),
                    )
                    computed += 1
    except Exception:
        logger.exception("refresh_opportunities: transaction failed")
        return {"computed": computed, "removed": removed, "skipped": skipped}

    logger.info(
        "refresh_opportunities: computed=%d removed=%d skipped=%d (of %d queries)",
        computed, removed, skipped, len(queries),
    )
    return {"computed": computed, "removed": removed, "skipped": skipped}


def pg_read_watchlist() -> list[str]:
    """Read watchlist queries from Postgres; fall back to watchlist.txt on error.

    After 3 consecutive failures, skips Postgres for 10 minutes to avoid
    flooding Supabase's circuit breaker.
    """
    global _pg_failure_count, _pg_backoff_until

    now = datetime.now()
    if _pg_backoff_until is not None and now < _pg_backoff_until:
        remaining = int((_pg_backoff_until - now).total_seconds() / 60)
        logger.warning("pg_read_watchlist: in backoff, %d min remaining — reading from file", remaining)
        return _read_watchlist()

    try:
        with pg_read() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT query FROM watchlist ORDER BY id")
                result = [r["query"] for r in cur.fetchall()]
        _pg_failure_count = 0
        _pg_backoff_until = None
        return result
    except Exception as exc:
        _pg_failure_count += 1
        logger.warning(
            "pg_read_watchlist: Postgres unavailable (%s) — failure %d/%d",
            exc, _pg_failure_count, _PG_BACKOFF_THRESHOLD,
        )
        if _pg_failure_count >= _PG_BACKOFF_THRESHOLD:
            _pg_backoff_until = now + timedelta(minutes=_PG_BACKOFF_MINUTES)
            logger.warning(
                "pg_read_watchlist: %d consecutive failures — backing off for %d minutes",
                _pg_failure_count, _PG_BACKOFF_MINUTES,
            )
            _pg_failure_count = 0
        return _read_watchlist()


def pg_watchlist_add(query: str) -> bool:
    """Insert query into Postgres watchlist. Returns True if inserted, False if duplicate."""
    with pg_write() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO watchlist (query) VALUES (%s) ON CONFLICT DO NOTHING", (query,))
            return cur.rowcount > 0


def pg_watchlist_remove(query: str) -> bool:
    """Delete query from Postgres watchlist. Returns True if deleted, False if not found."""
    with pg_write() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM watchlist WHERE query = %s", (query,))
            return cur.rowcount > 0


# ── Watchlist run tracking ────────────────────────────────────────────────────

def ensure_watchlist_runs_table() -> None:
    with pg_write() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS watchlist_runs (
                    query TEXT PRIMARY KEY,
                    last_run_at TIMESTAMP NOT NULL,
                    listings_found INTEGER NOT NULL DEFAULT 0
                )
            """)


def _upsert_watchlist_run(query: str, now: datetime, listings_found: int | None) -> None:
    try:
        with pg_write() as conn:
            with conn.cursor() as cur:
                if listings_found is not None:
                    cur.execute(
                        """INSERT INTO watchlist_runs (query, last_run_at, listings_found)
                               VALUES (%s, %s, %s)
                               ON CONFLICT (query) DO UPDATE
                                   SET last_run_at = EXCLUDED.last_run_at,
                                       listings_found = EXCLUDED.listings_found""",
                        (query, now, listings_found),
                    )
                else:
                    cur.execute(
                        """INSERT INTO watchlist_runs (query, last_run_at, listings_found)
                               VALUES (%s, %s, 0)
                               ON CONFLICT (query) DO UPDATE
                                   SET last_run_at = EXCLUDED.last_run_at""",
                        (query, now),
                    )
    except Exception:
        logger.warning("Could not upsert watchlist_run for %r", query)


# ── Background tasks ─────────────────────────────────────────────────────────

def run_scrape_sold() -> dict[str, int]:
    """Sold-price scrape across Plick (Såld detection), Tradera (ended auctions), and eBay (LH_Sold).

    Walks every watchlist query, ingests sold rows with ``status='sold'``, and
    finally refreshes arbitrage_opportunities so sold medians flow through.

    Plick sold-detection runs inside ``run_scrape_update`` (the badge surfaces
    on the listing detail page); this task focuses on Tradera + eBay sold
    histories, which are reachable only via dedicated search URLs.
    """
    try:
        queries = pg_read_watchlist()
        if not queries:
            logger.info("scrape/sold: watchlist empty")
            return {"sold_ingested": 0, "queries": 0}

        sold_total = 0
        for query in queries:
            for scraper_label, fn in [
                ("tradera-ended", tradera.scrape_query_ended if _tradera_available else None),
                ("ebay-sold", ebay.scrape_query_ended if _ebay_available else None),
            ]:
                if fn is None:
                    continue
                try:
                    sold_listings = fn(query)
                except Exception:
                    logger.exception("scrape/sold: %s failed for %r", scraper_label, query)
                    continue
                if sold_listings:
                    _write_to_pg(sold_listings)
                    sold_total += len(sold_listings)

        try:
            refresh_opportunities()
        except Exception:
            logger.exception("scrape/sold: refresh_opportunities failed (non-fatal)")

        logger.info("scrape/sold complete: %d sold listings ingested across %d queries", sold_total, len(queries))
        return {"sold_ingested": sold_total, "queries": len(queries)}
    except Exception:
        logger.exception("scrape/sold: unhandled error, task aborted")
        return {"sold_ingested": 0, "queries": 0}


def run_scrape_new() -> None:
    """Scrape each watchlist query, saving only listing IDs not already in Postgres."""
    try:
        queries = pg_read_watchlist()
        if not queries:
            logger.info("scrape/new: watchlist is empty")
            return

        with pg_read() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT listing_id FROM listings")
                known_ids: set[str] = {r["listing_id"] for r in cur.fetchall()}

        now = datetime.now()
        total = 0
        all_new: list[Listing] = []
        _new_scrapers = [plick.scrape_query_new_only, vinted.scrape_query_new_only]
        if _tradera_available:
            _new_scrapers.append(tradera.scrape_query_new_only)
        if _blocket_available:
            _new_scrapers.append(blocket.scrape_query_new_only)
        if _ebay_available:
            _new_scrapers.append(ebay.scrape_query_new_only)
        for query in queries:
            query_count = 0
            for scraper in _new_scrapers:
                try:
                    listings = scraper(query, known_ids)
                except Exception:
                    logger.exception("scrape/new: scraper failed for %r", query)
                    continue
                if listings:
                    _write_to_pg(listings)
                    known_ids.update(l.listing_id for l in listings)
                    all_new.extend(listings)
                    query_count += len(listings)
                    total += len(listings)
            _upsert_watchlist_run(query, now, listings_found=query_count)
        logger.info("scrape/new complete: %d new listings", total)
        if _check_alerts is not None and all_new:
            try:
                _check_alerts(all_new)
            except Exception:
                logger.exception("check_alerts raised an error")
        try:
            refresh_opportunities()
        except Exception:
            logger.exception("scrape/new: refresh_opportunities failed (non-fatal)")
    except Exception:
        logger.exception("scrape/new: unhandled error, task aborted")


def _mark_removed_pg(listing_id: str, now: datetime) -> None:
    try:
        with pg_write() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE listings SET status = 'removed', last_seen = %s WHERE listing_id = %s",
                    (now, listing_id),
                )
    except psycopg2.OperationalError as exc:
        logger.warning("Postgres unreachable (%s) — SQLite fallback: marking %s removed", exc, listing_id)
        from bourse.db import get_connection, DB_PATH
        with get_connection(DB_PATH) as conn:
            conn.execute(
                "UPDATE listings SET status = 'removed', last_seen = ? WHERE listing_id = ?",
                (now.isoformat(), listing_id),
            )


SOLD_PRICE_ESTIMATE_DISCOUNT = 0.10  # 10% off last asking price for sold-without-final-price


def _mark_sold_pg(listing_id: str, price_sek: int | None, now: datetime) -> None:
    """Flip a listing to status='sold' and record a final-sale snapshot.

    When ``price_sek`` is None (e.g. Vinted 410 — we know it sold but have no
    transaction price), fall back to ``last known snapshot price × 0.9``.
    The result is still inserted as a snapshot so the row shows up in the Sold
    tab with a plausible price; ``sold_quality='estimated'`` on the API side
    makes the uncertainty visible.
    """
    try:
        with pg_write() as conn:
            with conn.cursor() as cur:
                # If no explicit price, estimate from last snapshot
                if price_sek is None:
                    cur.execute(
                        "SELECT price_sek FROM listing_snapshots WHERE listing_id = %s ORDER BY scraped_at DESC LIMIT 1",
                        (listing_id,),
                    )
                    row = cur.fetchone()
                    last_price = row["price_sek"] if row else None
                    if last_price is not None:
                        price_sek = int(round(last_price * (1 - SOLD_PRICE_ESTIMATE_DISCOUNT)))

                cur.execute(
                    "UPDATE listings SET status = 'sold', last_seen = %s WHERE listing_id = %s",
                    (now, listing_id),
                )
                if price_sek is not None:
                    cur.execute(
                        "INSERT INTO listing_snapshots (listing_id, scraped_at, price_sek, likes, views, position_in_search) VALUES (%s,%s,%s,NULL,NULL,NULL)",
                        (listing_id, now, price_sek),
                    )
    except psycopg2.OperationalError as exc:
        logger.warning("Postgres unreachable (%s) — SQLite fallback: marking %s sold", exc, listing_id)
        from bourse.db import get_connection, DB_PATH
        with get_connection(DB_PATH) as conn:
            if price_sek is None:
                row = conn.execute(
                    "SELECT price_sek FROM listing_snapshots WHERE listing_id = ? ORDER BY scraped_at DESC LIMIT 1",
                    (listing_id,),
                ).fetchone()
                if row is not None:
                    price_sek = int(round(row["price_sek"] * (1 - SOLD_PRICE_ESTIMATE_DISCOUNT)))
            conn.execute(
                "UPDATE listings SET status = 'sold', last_seen = ? WHERE listing_id = ?",
                (now.isoformat(), listing_id),
            )
            if price_sek is not None:
                conn.execute(
                    "INSERT INTO listing_snapshots (listing_id, scraped_at, price_sek, likes, views, position_in_search) VALUES (?,?,?,NULL,NULL,NULL)",
                    (listing_id, now.isoformat(), price_sek),
                )


def _append_snapshot_pg(listing_id: str, price_sek: int, likes: int | None, now: datetime) -> None:
    try:
        with pg_write() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO listing_snapshots (listing_id, scraped_at, price_sek, likes, views, position_in_search) VALUES (%s,%s,%s,%s,NULL,NULL)",
                    (listing_id, now, price_sek, likes),
                )
                cur.execute(
                    "UPDATE listings SET last_seen = %s WHERE listing_id = %s",
                    (now, listing_id),
                )
    except psycopg2.OperationalError as exc:
        logger.warning("Postgres unreachable (%s) — SQLite fallback for snapshot %s", exc, listing_id)
        from bourse.db import get_connection, DB_PATH
        with get_connection(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO listing_snapshots (listing_id, scraped_at, price_sek, likes, views, position_in_search) VALUES (?,?,?,?,NULL,NULL)",
                (listing_id, now.isoformat(), price_sek, likes),
            )
            conn.execute(
                "UPDATE listings SET last_seen = ? WHERE listing_id = ?",
                (now.isoformat(), listing_id),
            )


def run_scrape_update() -> None:
    """Re-fetch each active listing's page to record its current price and likes."""
    try:
        with pg_read() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT listing_id, url FROM listings WHERE status = 'active'")
                active = [(r["listing_id"], r["url"]) for r in cur.fetchall()]

        if not active:
            logger.info("scrape/update: no active listings")
            return

        now = datetime.now()
        updated = removed = sold_count = 0
        for listing_id, url in active:
            try:
                is_sold = False
                if listing_id.startswith("plick:"):
                    price, likes, is_gone, is_sold = plick.fetch_listing(url)
                elif listing_id.startswith("vinted:"):
                    price, likes, is_gone, is_sold = vinted.fetch_listing(listing_id)
                elif listing_id.startswith("tradera:"):
                    if not _tradera_available:
                        logger.debug("scrape/update: tradera unavailable, skipping %s", listing_id)
                        continue
                    _p, _l = tradera.fetch_listing(listing_id, url)
                    price, likes, is_gone = _p, _l, (_p is None)
                elif listing_id.startswith("blocket:"):
                    if not _blocket_available:
                        logger.debug("scrape/update: blocket unavailable, skipping %s", listing_id)
                        continue
                    _p, _l = blocket.fetch_listing(listing_id, url)
                    price, likes, is_gone = _p, _l, (_p is None)
                elif listing_id.startswith("ebay:"):
                    if not _ebay_available:
                        logger.debug("scrape/update: ebay unavailable, skipping %s", listing_id)
                        continue
                    _p, _l = ebay.fetch_listing(listing_id, url)
                    price, likes, is_gone = _p, _l, (_p is None)
                else:
                    logger.warning("scrape/update: unknown platform for %s", listing_id)
                    continue
                if is_sold:
                    _mark_sold_pg(listing_id, price, now)
                    sold_count += 1
                elif is_gone:
                    _mark_removed_pg(listing_id, now)
                    removed += 1
                elif price is not None:
                    _append_snapshot_pg(listing_id, price, likes, now)
                    updated += 1
                else:
                    logger.warning("scrape/update: no price found for %s, skipping", listing_id)
            except Exception:
                logger.exception("scrape/update: failed for %s", listing_id)
        logger.info(
            "scrape/update complete: %d updated, %d sold, %d removed",
            updated, sold_count, removed,
        )
        for query in pg_read_watchlist():
            _upsert_watchlist_run(query, now, listings_found=None)
        try:
            refresh_opportunities()
        except Exception:
            logger.exception("scrape/update: refresh_opportunities failed (non-fatal)")
    except Exception:
        logger.exception("scrape/update: unhandled error, task aborted")
