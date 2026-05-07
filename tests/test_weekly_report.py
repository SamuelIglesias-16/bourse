from __future__ import annotations

from email.message import EmailMessage

import pytest

from bourse import weekly_report


def _sample_data() -> dict[str, object]:
    return {
        "top_new": [
            {
                "title": "Acne Studios jacket",
                "platform": "vinted",
                "price_sek": 1200,
                "likes": 18,
                "url": "https://example.com/1",
            }
        ],
        "price_drops": [
            {
                "title": "Margiela jeans",
                "platform": "tradera",
                "first_price": 2200,
                "latest_price": 1600,
                "drop_sek": 600,
                "url": "https://example.com/2",
            }
        ],
        "top_brand": {"brand": "acne studios", "new_listings": 7},
        "total_new": 42,
        "stale_count": 9,
    }


def test_render_html_contains_dark_and_orange_sections() -> None:
    html = weekly_report._render_html(_sample_data())

    assert "background: #111315" in html
    assert "#ff8a3d" in html
    assert "Top 5 Most Liked New Listings" in html
    assert "Top 5 Biggest Price Drops" in html
    assert "acne studios" in html
    assert "42" in html


def test_render_html_contains_expected_sections() -> None:
    html = weekly_report._render_html(_sample_data())

    assert "Secondhand market pulse" in html
    assert "New Listings" in html
    assert "Top Brand" in html
    assert "Stale Listings" in html
    assert "Acne Studios jacket" in html
    assert "Margiela jeans" in html


def test_fetch_weekly_data_aggregates_query_results(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[str] = []

    class FakeCursor:
        def __init__(self) -> None:
            self._index = 0
            self._responses = [
                [{"title": "A", "platform": "vinted", "url": "u", "price_sek": 100, "likes": 5, "first_seen": "x"}],
                [{"title": "B", "platform": "tradera", "url": "u2", "first_price": 300, "latest_price": 200, "drop_sek": 100}],
                {"brand": "acne studios", "new_listings": 4},
                {"total_new": 11},
                {"stale_count": 3},
            ]

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def execute(self, sql: str) -> None:
            executed.append(sql)

        def fetchall(self) -> list[dict[str, object]]:
            value = self._responses[self._index]
            self._index += 1
            return value  # type: ignore[return-value]

        def fetchone(self) -> dict[str, object]:
            value = self._responses[self._index]
            self._index += 1
            return value  # type: ignore[return-value]

    class FakeConn:
        autocommit = False

        def __enter__(self) -> FakeConn:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(weekly_report.psycopg2, "connect", lambda *args, **kwargs: FakeConn())

    data = weekly_report._fetch_weekly_data("postgres://example")

    assert len(executed) == 5
    assert data["top_brand"] == {"brand": "acne studios", "new_listings": 4}
    assert data["total_new"] == 11
    assert data["stale_count"] == 3


def test_send_weekly_report_sends_via_gmail_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, object] = {}

    class FakeSMTP:
        def __init__(self, host: str, port: int) -> None:
            sent["host"] = host
            sent["port"] = port

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def starttls(self) -> None:
            sent["starttls"] = True

        def login(self, username: str, password: str) -> None:
            sent["login"] = (username, password)

        def send_message(self, message: EmailMessage) -> None:
            sent["message"] = message

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("EMAIL_FROM", "from@example.com")
    monkeypatch.setenv("EMAIL_TO", "to@example.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr(weekly_report, "_fetch_weekly_data", lambda _: _sample_data())
    monkeypatch.setattr(weekly_report.smtplib, "SMTP", FakeSMTP)

    weekly_report.send_weekly_report()

    message = sent["message"]
    assert sent["host"] == "smtp.gmail.com"
    assert sent["port"] == 587
    assert sent["starttls"] is True
    assert sent["login"] == ("from@example.com", "secret")
    assert isinstance(message, EmailMessage)
    assert message["Subject"] == "Bourse Weekly Report"
    assert message["To"] == "to@example.com"
    assert "text/html" in message.as_string()


def test_send_weekly_report_logs_and_skips_without_email_env(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    called = False

    class FakeSMTP:
        def __init__(self, *args: object, **kwargs: object) -> None:
            nonlocal called
            called = True

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.delenv("EMAIL_FROM", raising=False)
    monkeypatch.delenv("EMAIL_TO", raising=False)
    monkeypatch.delenv("EMAIL_PASSWORD", raising=False)
    monkeypatch.setattr(weekly_report, "_fetch_weekly_data", lambda _: _sample_data())
    monkeypatch.setattr(weekly_report.smtplib, "SMTP", FakeSMTP)

    with caplog.at_level("INFO"):
        weekly_report.send_weekly_report()

    assert not called
    assert "would have sent weekly report" in caplog.text
    assert "Weekly report HTML" in caplog.text


def test_fetch_weekly_data_handles_empty_database_results(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCursor:
        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def execute(self, sql: str) -> None:
            return None

        def fetchall(self) -> list[dict[str, object]]:
            return []

        def fetchone(self):
            return None

    class FakeConn:
        def __enter__(self) -> FakeConn:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(weekly_report.psycopg2, "connect", lambda *args, **kwargs: FakeConn())

    data = weekly_report._fetch_weekly_data("postgres://example")
    html = weekly_report._render_html(data)

    assert data["top_new"] == []
    assert data["price_drops"] == []
    assert data["top_brand"] == {"brand": "unknown", "new_listings": 0}
    assert data["total_new"] == 0
    assert data["stale_count"] == 0
    assert html.count("No data this week.") == 2


def test_send_weekly_report_subject_line_format(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, object] = {}

    class FakeSMTP:
        def __init__(self, host: str, port: int) -> None:
            return None

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def starttls(self) -> None:
            return None

        def login(self, username: str, password: str) -> None:
            return None

        def send_message(self, message: EmailMessage) -> None:
            sent["subject"] = message["Subject"]

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("EMAIL_FROM", "from@example.com")
    monkeypatch.setenv("EMAIL_TO", "to@example.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr(weekly_report, "_fetch_weekly_data", lambda _: _sample_data())
    monkeypatch.setattr(weekly_report.smtplib, "SMTP", FakeSMTP)

    weekly_report.send_weekly_report()

    assert sent["subject"] == "Bourse Weekly Report"
