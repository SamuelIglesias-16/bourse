"""Price alert checks and Telegram delivery."""

from __future__ import annotations

import logging
import os

import httpx

from bourse.db import DB_PATH, get_connection
from bourse.models import Listing

logger = logging.getLogger(__name__)

PRICE_ALERTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT,
    size        TEXT,
    max_price   INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

CATEGORIES: dict[str, list[str]] = {
    "hoodie": ["hoodie", "huvtröja", "huvtrojor", "sweatshirt"],
    "longsleeve": ["longsleeve", "long sleeve", "långärmad", "langarmad"],
    "t-shirt": ["t-shirt", "tshirt", "t shirt"],
    "jeans": ["jeans", "denim"],
    "jacket": ["jacka", "jacket", "kappa", "coat", "väst"],
}


def _ensure_price_alerts_table() -> None:
    with get_connection(DB_PATH) as conn:
        conn.execute(PRICE_ALERTS_SCHEMA)


def _detect_category(title: str) -> str:
    lowered = title.lower()
    for name, keywords in CATEGORIES.items():
        if any(keyword in lowered for keyword in keywords):
            return name
    return "other"


def _load_active_alerts() -> list[dict[str, object]]:
    _ensure_price_alerts_table()
    with get_connection(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, category, size, max_price, active
              FROM price_alerts
             WHERE active = 1
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _matches_alert(listing: Listing, alert: dict[str, object]) -> bool:
    if listing.price_sek >= int(alert["max_price"]):
        return False

    category = str(alert["category"]).strip().lower() if alert["category"] else ""
    if category and _detect_category(listing.title) != category:
        return False

    size = str(alert["size"]).strip().lower() if alert["size"] else ""
    listing_size = (listing.size or "").strip().lower()
    if size and listing_size != size:
        return False

    return True


def _format_alert_message(listing: Listing) -> str:
    size = listing.size or "-"
    likes = listing.likes if listing.likes is not None else 0
    return (
        "🔔 PRICE ALERT\n"
        f"{listing.title} — {listing.price_sek}kr\n"
        f"Platform: {listing.platform}\n"
        f"Size: {size}\n"
        f"Likes: {likes}\n"
        f"{listing.url}"
    )


def send_telegram(message: str) -> None:
    """Send a Telegram message if bot credentials are configured."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("Telegram env vars missing; skipping alert send")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = httpx.post(
        url,
        json={"chat_id": chat_id, "text": message},
        timeout=15,
    )
    response.raise_for_status()


def check_alerts(new_listings: list[Listing]) -> None:
    """Check incoming listings against active price alerts and notify matches."""
    if not new_listings:
        _ensure_price_alerts_table()
        return

    alerts = _load_active_alerts()
    if not alerts:
        return

    for listing in new_listings:
        for alert in alerts:
            if _matches_alert(listing, alert):
                send_telegram(_format_alert_message(listing))
