from __future__ import annotations

from datetime import datetime
from pathlib import Path

from bourse.db import get_connection, init_db
from bourse.ingest import ingest
from bourse.models import Listing


def _listing(
    *,
    listing_id: str = "vinted:1",
    brand: str | None = "Masion Margiela",
    size: str | None = "xxs/32/4",
    price_sek: int = 900,
    scraped_at: datetime | None = None,
    condition: str | None = "good",
) -> Listing:
    return Listing(
        listing_id=listing_id,
        platform="vinted",
        url=f"https://example.com/{listing_id}",
        title="Maison Margiela jeans",
        brand=brand,
        size=size,
        condition=condition,
        material=None,
        seller_name="samuel",
        seller_rating=None,
        posted_at=None,
        price_sek=price_sek,
        likes=3,
        views=None,
        position_in_search=1,
        scraped_at=scraped_at or datetime(2026, 5, 7, 12, 0, 0),
    )


def test_normalize_brand_and_size_are_applied_before_db_write(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "ingest.db"
    monkeypatch.setattr("bourse.ingest.DB_PATH", db_path)
    init_db(db_path)

    summary = ingest([_listing(brand="Masion Margiela", size="xxs/32/4")])

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT brand, size FROM listings WHERE listing_id = ?",
            ("vinted:1",),
        ).fetchone()

    assert summary.new == 1
    assert row["brand"] == "maison margiela"
    assert row["size"] == "XS"


def test_price_filter_rejects_out_of_range_listings(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "prices.db"
    monkeypatch.setattr("bourse.ingest.DB_PATH", db_path)
    init_db(db_path)

    summary = ingest(
        [
            _listing(listing_id="vinted:low", price_sek=199),
            _listing(listing_id="vinted:high", price_sek=50001),
        ]
    )

    with get_connection(db_path) as conn:
        listing_count = conn.execute("SELECT COUNT(*) AS c FROM listings").fetchone()["c"]
        snapshot_count = conn.execute("SELECT COUNT(*) AS c FROM listing_snapshots").fetchone()["c"]

    assert summary.filtered_out == 2
    assert summary.new == 0
    assert summary.total_snapshots == 0
    assert listing_count == 0
    assert snapshot_count == 0


def test_duplicate_listing_id_updates_rather_than_inserts(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "duplicates.db"
    monkeypatch.setattr("bourse.ingest.DB_PATH", db_path)
    init_db(db_path)

    first = _listing(
        listing_id="vinted:dup",
        scraped_at=datetime(2026, 5, 7, 12, 0, 0),
        condition="good",
    )
    second = _listing(
        listing_id="vinted:dup",
        scraped_at=datetime(2026, 5, 8, 12, 0, 0),
        condition="excellent",
    )

    summary = ingest([first, second])

    with get_connection(db_path) as conn:
        listing_count = conn.execute("SELECT COUNT(*) AS c FROM listings").fetchone()["c"]
        snapshot_count = conn.execute(
            "SELECT COUNT(*) AS c FROM listing_snapshots WHERE listing_id = ?",
            ("vinted:dup",),
        ).fetchone()["c"]
        row = conn.execute(
            "SELECT condition, first_seen, last_seen FROM listings WHERE listing_id = ?",
            ("vinted:dup",),
        ).fetchone()

    assert summary.new == 1
    assert summary.updated == 1
    assert summary.total_snapshots == 2
    assert listing_count == 1
    assert snapshot_count == 2
    assert row["condition"] == "excellent"
    assert row["first_seen"] == "2026-05-07T12:00:00"
    assert row["last_seen"] == "2026-05-08T12:00:00"
