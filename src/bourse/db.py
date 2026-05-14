"""SQLite schema and connection helpers."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "bourse.db"


def get_connection(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: Path = DB_PATH) -> None:
    """Create all tables if they don't exist (idempotent)."""
    with get_connection(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS listings (
                listing_id      TEXT PRIMARY KEY,           -- e.g. "plick:12345678"
                platform        TEXT NOT NULL,              -- plick | vinted | blocket
                url             TEXT NOT NULL,
                title           TEXT NOT NULL,
                brand           TEXT,                       -- normalized lowercase
                size            TEXT,
                condition       TEXT,
                material        TEXT,
                seller_name     TEXT NOT NULL,
                seller_rating   REAL,
                posted_at       TIMESTAMP,                    -- listing date; NULL for platforms that don't expose it
                first_seen      TIMESTAMP NOT NULL,
                last_seen       TIMESTAMP NOT NULL,
                status          TEXT NOT NULL DEFAULT 'active',  -- active | sold | removed | unknown
                raw             TEXT,                       -- JSON blob for extra platform fields (location, category…)
                image_url       TEXT                        -- thumbnail URL when scraper exposes one (Blocket+, others null)
            );

            CREATE TABLE IF NOT EXISTS listing_snapshots (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id          TEXT NOT NULL REFERENCES listings(listing_id),
                scraped_at          TIMESTAMP NOT NULL,
                price_sek           INTEGER NOT NULL,
                likes               INTEGER,
                views               INTEGER,
                position_in_search  INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_listing_id
                ON listing_snapshots (listing_id, scraped_at DESC);
        """)
        # Migrations: add columns to existing databases that predate them
        for ddl in (
            "ALTER TABLE listings ADD COLUMN posted_at TIMESTAMP",
            "ALTER TABLE listings ADD COLUMN image_url TEXT",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists


if __name__ == "__main__":
    init_db()
    print(f"Database initialised at {DB_PATH.resolve()}")
