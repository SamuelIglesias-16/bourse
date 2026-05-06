"""One-time migration: copy all data from bourse.db (SQLite) → Postgres (DATABASE_URL)."""

import logging
import os
import sys
from datetime import datetime

import psycopg2
from dotenv import load_dotenv

from bourse.db import DB_PATH, get_connection

load_dotenv()
logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS listings (
    listing_id    TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    brand         TEXT,
    size          TEXT,
    condition     TEXT,
    material      TEXT,
    seller_name   TEXT NOT NULL,
    seller_rating REAL,
    posted_at     TIMESTAMP,
    first_seen    TIMESTAMP NOT NULL,
    last_seen     TIMESTAMP NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    raw           TEXT
);
CREATE TABLE IF NOT EXISTS listing_snapshots (
    id                 SERIAL PRIMARY KEY,
    listing_id         TEXT NOT NULL REFERENCES listings(listing_id),
    scraped_at         TIMESTAMP NOT NULL,
    price_sek          INTEGER NOT NULL,
    likes              INTEGER,
    views              INTEGER,
    position_in_search INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snapshots_listing_id
    ON listing_snapshots (listing_id, scraped_at DESC);
"""


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except (ValueError, TypeError):
        return None


def migrate() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL not set — check your .env file")
    if not DB_PATH.exists():
        sys.exit(f"SQLite database not found: {DB_PATH}")

    with get_connection(DB_PATH) as sq:
        listings = sq.execute("SELECT * FROM listings").fetchall()
        snapshots = sq.execute("SELECT * FROM listing_snapshots").fetchall()

    logger.info("Read %d listings, %d snapshots from SQLite", len(listings), len(snapshots))

    pg = psycopg2.connect(url)
    try:
        with pg.cursor() as cur:
            cur.execute(_DDL)
            for row in listings:
                cur.execute(
                    """
                    INSERT INTO listings
                        (listing_id, platform, url, title, brand, size, condition, material,
                         seller_name, seller_rating, posted_at, first_seen, last_seen, status, raw)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (listing_id) DO NOTHING
                    """,
                    (
                        row["listing_id"], row["platform"], row["url"], row["title"],
                        row["brand"], row["size"], row["condition"], row["material"],
                        row["seller_name"], row["seller_rating"],
                        _ts(row["posted_at"]), _ts(row["first_seen"]), _ts(row["last_seen"]),
                        row["status"], row["raw"],
                    ),
                )
            for row in snapshots:
                cur.execute(
                    """
                    INSERT INTO listing_snapshots
                        (listing_id, scraped_at, price_sek, likes, views, position_in_search)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        row["listing_id"], _ts(row["scraped_at"]),
                        row["price_sek"], row["likes"], row["views"], row["position_in_search"],
                    ),
                )
        pg.commit()
        print(f"✓ Migrated {len(listings)} listings and {len(snapshots)} snapshots to Postgres.")
    except Exception:
        pg.rollback()
        logger.exception("Migration failed — rolled back")
        raise
    finally:
        pg.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    migrate()
