from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from bourse import alerts
from bourse.models import Listing


def _ensure_alerts_table() -> None:
    alerts._ensure_price_alerts_table()


def _listing(
    *,
    title: str = "Acne Studios jeans",
    price_sek: int = 900,
    size: str | None = "M",
    likes: int | None = 3,
    platform: str = "vinted",
) -> Listing:
    return Listing(
        listing_id="vinted:1",
        platform=platform,
        url="https://example.com/listing/1",
        title=title,
        brand="acne studios",
        size=size,
        condition="good",
        material=None,
        seller_name="samuel",
        seller_rating=None,
        posted_at=None,
        price_sek=price_sek,
        likes=likes,
        views=None,
        position_in_search=1,
        scraped_at=datetime(2026, 5, 7, 12, 0, 0),
    )


def test_send_telegram_posts_expected_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, object] = {}

    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

    def fake_post(url: str, json: dict[str, object], timeout: int) -> DummyResponse:
        sent["url"] = url
        sent["json"] = json
        sent["timeout"] = timeout
        return DummyResponse()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-id")
    monkeypatch.setattr(alerts.httpx, "post", fake_post)

    alerts.send_telegram("hello")

    assert sent["url"] == "https://api.telegram.org/botbot-token/sendMessage"
    assert sent["json"] == {"chat_id": "chat-id", "text": "hello"}
    assert sent["timeout"] == 15


def test_send_telegram_warns_and_returns_without_env(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    called = False

    def fake_post(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(alerts, "load_dotenv", lambda: None)
    monkeypatch.setattr(alerts.httpx, "post", fake_post)

    with caplog.at_level("WARNING"):
        alerts.send_telegram("hello")

    assert not called
    assert "Telegram env vars missing" in caplog.text


def test_check_alerts_matches_price_category_and_size(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(alerts, "DB_PATH", tmp_path / "alerts.db")
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send_telegram", sent.append)
    _ensure_alerts_table()

    with alerts.get_connection(alerts.DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO price_alerts (category, size, max_price, active)
            VALUES (?, ?, ?, ?)
            """,
            ("jeans", "m", 1000, 1),
        )
        conn.execute(
            """
            INSERT INTO price_alerts (category, size, max_price, active)
            VALUES (?, ?, ?, ?)
            """,
            ("jacket", "m", 1000, 1),
        )
        conn.execute(
            """
            INSERT INTO price_alerts (category, size, max_price, active)
            VALUES (?, ?, ?, ?)
            """,
            ("jeans", "s", 1000, 1),
        )
        conn.execute(
            """
            INSERT INTO price_alerts (category, size, max_price, active)
            VALUES (?, ?, ?, ?)
            """,
            ("jeans", "m", 800, 1),
        )
        conn.execute(
            """
            INSERT INTO price_alerts (category, size, max_price, active)
            VALUES (?, ?, ?, ?)
            """,
            ("jeans", "m", 1000, 0),
        )

    alerts.check_alerts([_listing()])

    assert sent == [
        "🔔 PRICE ALERT\n"
        "Acne Studios jeans — 900kr\n"
        "Platform: vinted\n"
        "Size: M\n"
        "Likes: 3\n"
        "https://example.com/listing/1"
    ]


def test_check_alerts_creates_table_if_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(alerts, "DB_PATH", tmp_path / "missing.db")
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send_telegram", sent.append)

    alerts.check_alerts([_listing(price_sek=500)])

    with alerts.get_connection(alerts.DB_PATH) as conn:
        table = conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table' AND name = 'price_alerts'
            """
        ).fetchone()

    assert table is not None
    assert sent == []


def test_check_alerts_uses_zero_and_dash_for_missing_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(alerts, "DB_PATH", tmp_path / "format.db")
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send_telegram", sent.append)
    _ensure_alerts_table()

    with alerts.get_connection(alerts.DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO price_alerts (category, size, max_price, active)
            VALUES (?, ?, ?, ?)
            """,
            (None, None, 1000, 1),
        )

    alerts.check_alerts([_listing(size=None, likes=None, platform="tradera")])

    assert sent == [
        "🔔 PRICE ALERT\n"
        "Acne Studios jeans — 900kr\n"
        "Platform: tradera\n"
        "Size: -\n"
        "Likes: 0\n"
        "https://example.com/listing/1"
    ]


def test_inactive_alerts_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(alerts, "DB_PATH", tmp_path / "inactive.db")
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send_telegram", sent.append)
    _ensure_alerts_table()

    with alerts.get_connection(alerts.DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO price_alerts (category, size, max_price, active)
            VALUES (?, ?, ?, ?)
            """,
            ("jeans", "m", 1000, 0),
        )

    alerts.check_alerts([_listing()])

    assert sent == []


def test_alert_without_category_matches_any_listing_under_price(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(alerts, "DB_PATH", tmp_path / "any-category.db")
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send_telegram", sent.append)
    _ensure_alerts_table()

    with alerts.get_connection(alerts.DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO price_alerts (category, size, max_price, active)
            VALUES (?, ?, ?, ?)
            """,
            (None, "m", 1000, 1),
        )

    alerts.check_alerts([_listing(title="Maison Margiela trench coat")])

    assert sent == [
        "🔔 PRICE ALERT\n"
        "Maison Margiela trench coat — 900kr\n"
        "Platform: vinted\n"
        "Size: M\n"
        "Likes: 3\n"
        "https://example.com/listing/1"
    ]


@pytest.mark.xfail(reason="check_alerts currently sends notifications but does not return matched alerts")
def test_check_alerts_returns_list_of_matched_alerts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(alerts, "DB_PATH", tmp_path / "return-value.db")
    sent: list[str] = []
    monkeypatch.setattr(alerts, "send_telegram", sent.append)
    _ensure_alerts_table()

    with alerts.get_connection(alerts.DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO price_alerts (category, size, max_price, active)
            VALUES (?, ?, ?, ?)
            """,
            ("jeans", "m", 1000, 1),
        )

    matched = alerts.check_alerts([_listing()])

    assert matched == [
        {"category": "jeans", "size": "m", "max_price": 1000, "active": 1}
    ]
