"""Postgres-aware background scrape tasks for the API."""

import logging
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from bourse.models import Listing
import bourse.plick as plick
import bourse.vinted as vinted

load_dotenv()  # no-op on Railway; picks up .env locally
logger = logging.getLogger(__name__)
WATCHLIST_FILE = Path("watchlist.txt")
_CONNECT_TIMEOUT = 10  # seconds before psycopg2.connect() gives up


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
    cur.execute(
        """INSERT INTO listings
               (listing_id, platform, url, title, brand, size, condition, material,
                seller_name, seller_rating, posted_at, first_seen, last_seen, status, raw)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active','{}')
           ON CONFLICT (listing_id) DO UPDATE
               SET last_seen     = EXCLUDED.last_seen,
                   condition     = COALESCE(EXCLUDED.condition, listings.condition),
                   seller_rating = COALESCE(EXCLUDED.seller_rating, listings.seller_rating)""",
        (listing.listing_id, listing.platform, listing.url, listing.title,
         listing.brand, listing.size, listing.condition, listing.material,
         listing.seller_name, listing.seller_rating, listing.posted_at,
         listing.scraped_at, listing.scraped_at),
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

def run_scrape_new() -> None:
    """Scrape each watchlist query, saving only listing IDs not already in Postgres."""
    try:
        queries = _read_watchlist()
        if not queries:
            logger.info("scrape/new: watchlist is empty")
            return

        with pg_read() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT listing_id FROM listings")
                known_ids: set[str] = {r["listing_id"] for r in cur.fetchall()}

        now = datetime.now()
        total = 0
        for query in queries:
            query_count = 0
            for scraper in [plick.scrape_query_new_only, vinted.scrape_query_new_only]:
                try:
                    listings = scraper(query, known_ids)
                except Exception:
                    logger.exception("scrape/new: scraper failed for %r", query)
                    continue
                if listings:
                    _write_to_pg(listings)
                    known_ids.update(l.listing_id for l in listings)
                    query_count += len(listings)
                    total += len(listings)
            _upsert_watchlist_run(query, now, listings_found=query_count)
        logger.info("scrape/new complete: %d new listings", total)
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
        updated = removed = 0
        for listing_id, url in active:
            try:
                if listing_id.startswith("plick:"):
                    price, likes, is_gone = plick.fetch_listing(url)
                elif listing_id.startswith("vinted:"):
                    price, likes, is_gone = vinted.fetch_listing(listing_id)
                else:
                    logger.warning("scrape/update: unknown platform for %s", listing_id)
                    continue
                if is_gone:
                    _mark_removed_pg(listing_id, now)
                    removed += 1
                elif price is not None:
                    _append_snapshot_pg(listing_id, price, likes, now)
                    updated += 1
                else:
                    logger.warning("scrape/update: no price found for %s, skipping", listing_id)
            except Exception:
                logger.exception("scrape/update: failed for %s", listing_id)
        logger.info("scrape/update complete: %d updated, %d removed", updated, removed)
        for query in _read_watchlist():
            _upsert_watchlist_run(query, now, listings_found=None)
    except Exception:
        logger.exception("scrape/update: unhandled error, task aborted")
