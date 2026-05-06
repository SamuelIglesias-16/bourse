"""Write scraped listings to the database."""

import json
import logging
import sqlite3
from dataclasses import dataclass

from bourse.db import get_connection, DB_PATH
from bourse.models import Listing
from bourse.normalize import normalize_brand, normalize_size

logger = logging.getLogger(__name__)

PRICE_MIN = 200
PRICE_MAX = 50_000


@dataclass
class IngestSummary:
    new: int = 0
    updated: int = 0
    filtered_out: int = 0
    total_snapshots: int = 0

    def __str__(self) -> str:
        return (
            f"  New listings:      {self.new}\n"
            f"  Updated listings:  {self.updated}\n"
            f"  Filtered out:      {self.filtered_out}\n"
            f"  Snapshots written: {self.total_snapshots}"
        )


def _matches_query(listing: Listing, query: str) -> bool:
    """Return True if title or brand contains at least one keyword from *query*.

    Keywords are individual whitespace-separated tokens. Matching is
    case-insensitive. A listing with no brand is matched on title only.
    """
    keywords = [kw.lower() for kw in query.split() if kw]
    haystack = listing.title.lower()
    if listing.brand:
        haystack += " " + listing.brand.lower()
    return any(kw in haystack for kw in keywords)


def ingest(listings: list[Listing], query: str = "") -> IngestSummary:
    """Filter *listings* by *query* keywords, then upsert and snapshot survivors.

    Never updates or deletes existing snapshots — only inserts new ones.
    """
    summary = IngestSummary()
    if not listings:
        return summary

    listings = [_normalize_listing(listing) for listing in listings]

    if query:
        before = len(listings)
        listings = [l for l in listings if _matches_query(l, query)]
        summary.filtered_out += before - len(listings)
        if summary.filtered_out:
            logger.info(
                "Filtered out %d irrelevant listings (kept %d)",
                summary.filtered_out,
                len(listings),
            )

    before = len(listings)
    listings = [l for l in listings if PRICE_MIN <= l.price_sek <= PRICE_MAX]
    price_out = before - len(listings)
    if price_out:
        summary.filtered_out += price_out
        logger.info(
            "Filtered out %d listings with price outside %d–%d SEK",
            price_out, PRICE_MIN, PRICE_MAX,
        )

    if not listings:
        return summary

    with get_connection(DB_PATH) as conn:
        for listing in listings:
            _upsert_listing(conn, listing, summary)
            _insert_snapshot(conn, listing)
            summary.total_snapshots += 1

    return summary


def _normalize_listing(listing: Listing) -> Listing:
    """Return a copy with brand/size normalized before filtering or writes."""
    return listing.model_copy(
        update={
            "brand": normalize_brand(listing.brand),
            "size": normalize_size(listing.size),
        }
    )


def _upsert_listing(
    conn: sqlite3.Connection, listing: Listing, summary: IngestSummary
) -> None:
    row = conn.execute(
        "SELECT listing_id FROM listings WHERE listing_id = ?",
        (listing.listing_id,),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO listings (
                listing_id, platform, url, title, brand, size, condition,
                material, seller_name, seller_rating, posted_at,
                first_seen, last_seen, status, raw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                listing.listing_id,
                listing.platform,
                listing.url,
                listing.title,
                listing.brand,
                listing.size,
                listing.condition,
                listing.material,
                listing.seller_name,
                listing.seller_rating,
                listing.posted_at.isoformat() if listing.posted_at else None,
                listing.scraped_at.isoformat(),
                listing.scraped_at.isoformat(),
                json.dumps({}),
            ),
        )
        summary.new += 1
        logger.debug("new listing: %s", listing.listing_id)
    else:
        # Refresh mutable fields that can change between scrapes
        conn.execute(
            """
            UPDATE listings
               SET last_seen     = ?,
                   condition     = COALESCE(?, condition),
                   seller_rating = COALESCE(?, seller_rating)
             WHERE listing_id = ?
            """,
            (
                listing.scraped_at.isoformat(),
                listing.condition,
                listing.seller_rating,
                listing.listing_id,
            ),
        )
        summary.updated += 1


def _insert_snapshot(conn: sqlite3.Connection, listing: Listing) -> None:
    conn.execute(
        """
        INSERT INTO listing_snapshots
            (listing_id, scraped_at, price_sek, likes, views, position_in_search)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            listing.listing_id,
            listing.scraped_at.isoformat(),
            listing.price_sek,
            listing.likes,
            listing.views,
            listing.position_in_search,
        ),
    )
