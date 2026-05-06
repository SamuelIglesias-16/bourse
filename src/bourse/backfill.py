"""Backfill functions to normalize existing brand/size data in Postgres."""

import logging

from dotenv import load_dotenv

from bourse.normalize import normalize_brand, normalize_size
from bourse.scrape_tasks import pg_read, pg_write

load_dotenv()
logger = logging.getLogger(__name__)


def backfill_brands() -> int:
    """Normalize brand for every listing in Postgres. Returns count of rows updated."""
    with pg_read() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT listing_id, brand FROM listings")
            rows = cur.fetchall()

    updates = [
        (normalize_brand(r["brand"]) or None, r["listing_id"])
        for r in rows
        if normalize_brand(r["brand"]) != (r["brand"] or "")
    ]
    if not updates:
        return 0

    with pg_write() as conn:
        with conn.cursor() as cur:
            for new_brand, listing_id in updates:
                cur.execute(
                    "UPDATE listings SET brand = %s WHERE listing_id = %s",
                    (new_brand, listing_id),
                )
    logger.info("backfill_brands: updated %d rows", len(updates))
    return len(updates)


def backfill_sizes() -> int:
    """Normalize size for every listing in Postgres. Returns count of rows updated."""
    with pg_read() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT listing_id, size FROM listings")
            rows = cur.fetchall()

    updates = [
        (normalize_size(r["size"]) or None, r["listing_id"])
        for r in rows
        if normalize_size(r["size"]) != (r["size"] or "")
    ]
    if not updates:
        return 0

    with pg_write() as conn:
        with conn.cursor() as cur:
            for new_size, listing_id in updates:
                cur.execute(
                    "UPDATE listings SET size = %s WHERE listing_id = %s",
                    (new_size, listing_id),
                )
    logger.info("backfill_sizes: updated %d rows", len(updates))
    return len(updates)
