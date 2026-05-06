"""Weekly HTML email report for Postgres-backed market data."""

from __future__ import annotations

import logging
import os
import smtplib
from contextlib import contextmanager
from email.message import EmailMessage
from html import escape
from typing import Any, Generator

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 10
_TOP_NEW_SQL = """
    WITH latest AS (
        SELECT DISTINCT ON (listing_id) listing_id, price_sek, COALESCE(likes, 0) AS likes
          FROM listing_snapshots
         ORDER BY listing_id, scraped_at DESC
    )
    SELECT l.title, l.platform, l.url,
           lt.price_sek, lt.likes, l.first_seen
      FROM listings l
      JOIN latest lt ON lt.listing_id = l.listing_id
     WHERE l.first_seen >= NOW() - INTERVAL '7 days'
     ORDER BY lt.likes DESC, l.first_seen DESC
     LIMIT 5
"""
_PRICE_DROPS_SQL = """
    WITH ranked AS (
        SELECT listing_id, price_sek,
               ROW_NUMBER() OVER (PARTITION BY listing_id ORDER BY scraped_at ASC)  AS rn_first,
               ROW_NUMBER() OVER (PARTITION BY listing_id ORDER BY scraped_at DESC) AS rn_last
          FROM listing_snapshots
    ),
    prices AS (
        SELECT listing_id,
               MAX(CASE WHEN rn_first = 1 THEN price_sek END) AS first_price,
               MAX(CASE WHEN rn_last = 1 THEN price_sek END)  AS latest_price
          FROM ranked
         GROUP BY listing_id
    )
    SELECT l.title, l.platform, l.url,
           p.first_price, p.latest_price,
           (p.first_price - p.latest_price) AS drop_sek
      FROM listings l
      JOIN prices p ON p.listing_id = l.listing_id
     WHERE l.first_seen >= NOW() - INTERVAL '7 days'
       AND p.first_price > p.latest_price
     ORDER BY drop_sek DESC, l.first_seen DESC
     LIMIT 5
"""
_TOP_BRAND_SQL = """
    SELECT COALESCE(NULLIF(TRIM(brand), ''), 'unknown') AS brand,
           COUNT(*) AS new_listings
      FROM listings
     WHERE first_seen >= NOW() - INTERVAL '7 days'
     GROUP BY 1
     ORDER BY new_listings DESC, brand ASC
     LIMIT 1
"""
_TOTAL_NEW_SQL = """
    SELECT COUNT(*) AS total_new
      FROM listings
     WHERE first_seen >= NOW() - INTERVAL '7 days'
"""
_STALE_SQL = """
    WITH latest AS (
        SELECT DISTINCT ON (listing_id) listing_id, COALESCE(likes, 0) AS likes
          FROM listing_snapshots
         ORDER BY listing_id, scraped_at DESC
    )
    SELECT COUNT(*) AS stale_count
      FROM listings l
      JOIN latest lt ON lt.listing_id = l.listing_id
     WHERE l.status = 'active'
       AND l.first_seen < NOW() - INTERVAL '14 days'
       AND lt.likes < 3
"""


@contextmanager
def _pg_read(database_url: str) -> Generator[psycopg2.extensions.connection, None, None]:  # type: ignore[name-defined]
    conn = psycopg2.connect(
        database_url,
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=_CONNECT_TIMEOUT,
    )
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _fetch_weekly_data(database_url: str) -> dict[str, Any]:
    with _pg_read(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(_TOP_NEW_SQL)
            top_new = [dict(row) for row in cur.fetchall()]

            cur.execute(_PRICE_DROPS_SQL)
            price_drops = [dict(row) for row in cur.fetchall()]

            cur.execute(_TOP_BRAND_SQL)
            top_brand = dict(cur.fetchone() or {"brand": "unknown", "new_listings": 0})

            cur.execute(_TOTAL_NEW_SQL)
            total_new = int((cur.fetchone() or {"total_new": 0})["total_new"])

            cur.execute(_STALE_SQL)
            stale_count = int((cur.fetchone() or {"stale_count": 0})["stale_count"])

    return {
        "top_new": top_new,
        "price_drops": price_drops,
        "top_brand": top_brand,
        "total_new": total_new,
        "stale_count": stale_count,
    }


def _render_rows(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    if not rows:
        return "<p class='empty'>No data this week.</p>"
    head = "".join(f"<th>{escape(label)}</th>" for label, _ in columns)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{escape(str(row.get(key, '')))}</td>" for _, key in columns)
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        "<table><thead><tr>"
        f"{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
    )


def _render_html(data: dict[str, Any]) -> str:
    top_new_rows = [
        {
            "title": row["title"],
            "platform": row["platform"],
            "price": f"{row['price_sek']} kr",
            "likes": row["likes"],
        }
        for row in data["top_new"]
    ]
    drop_rows = [
        {
            "title": row["title"],
            "platform": row["platform"],
            "first_price": f"{row['first_price']} kr",
            "latest_price": f"{row['latest_price']} kr",
            "drop": f"{row['drop_sek']} kr",
        }
        for row in data["price_drops"]
    ]
    brand = data["top_brand"]["brand"]
    brand_count = data["top_brand"]["new_listings"]
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Bourse Weekly Report</title>
  <style>
    body {{ margin: 0; background: #111315; color: #f5f1ea; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
    .wrap {{ max-width: 880px; margin: 0 auto; padding: 32px 20px 48px; }}
    .hero {{ background: linear-gradient(135deg, #1a1d20 0%, #121416 100%); border: 1px solid #2a2d31; border-radius: 20px; padding: 28px; }}
    .eyebrow {{ color: #ff8a3d; font-size: 12px; font-weight: 700; letter-spacing: 0.16em; text-transform: uppercase; }}
    h1, h2 {{ margin: 0; }}
    h1 {{ font-size: 32px; margin-top: 8px; }}
    h2 {{ font-size: 18px; margin-bottom: 14px; color: #ffb172; }}
    p {{ color: #c9c1b7; line-height: 1.5; }}
    .grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin: 22px 0 6px; }}
    .card {{ background: #171a1d; border: 1px solid #2b2f33; border-radius: 16px; padding: 18px; }}
    .metric {{ color: #ff8a3d; font-size: 28px; font-weight: 800; margin-top: 8px; }}
    .label {{ color: #c9c1b7; font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em; }}
    .section {{ margin-top: 22px; background: #171a1d; border: 1px solid #2b2f33; border-radius: 18px; padding: 22px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 12px 10px; border-bottom: 1px solid #2a2d31; }}
    th {{ color: #ffb172; font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; }}
    td {{ color: #f5f1ea; font-size: 14px; }}
    .empty {{ margin: 0; color: #8e8a84; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div class="eyebrow">Bourse Weekly</div>
      <h1>Secondhand market pulse</h1>
      <p>Fresh inventory, early engagement, and price movement from the last 7 days.</p>
      <div class="grid">
        <div class="card">
          <div class="label">New Listings</div>
          <div class="metric">{data["total_new"]}</div>
        </div>
        <div class="card">
          <div class="label">Top Brand</div>
          <div class="metric">{escape(str(brand))}</div>
          <p>{brand_count} new listings this week</p>
        </div>
        <div class="card">
          <div class="label">Stale Listings</div>
          <div class="metric">{data["stale_count"]}</div>
          <p>Active listings older than 14 days with fewer than 3 likes.</p>
        </div>
      </div>
    </div>

    <div class="section">
      <h2>Top 5 Most Liked New Listings</h2>
      {_render_rows(top_new_rows, [("Title", "title"), ("Platform", "platform"), ("Price", "price"), ("Likes", "likes")])}
    </div>

    <div class="section">
      <h2>Top 5 Biggest Price Drops</h2>
      {_render_rows(drop_rows, [("Title", "title"), ("Platform", "platform"), ("First", "first_price"), ("Latest", "latest_price"), ("Drop", "drop")])}
    </div>
  </div>
</body>
</html>
"""


def send_weekly_report() -> None:
    """Fetch this week's Postgres data, render an HTML email, and send it."""
    email_from = os.environ.get("EMAIL_FROM")
    email_to = os.environ.get("EMAIL_TO")
    email_password = os.environ.get("EMAIL_PASSWORD")
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        logger.warning("DATABASE_URL missing; skipping weekly report")
        return

    data = _fetch_weekly_data(database_url)
    html_body = _render_html(data)
    subject = "Bourse Weekly Report"

    if not email_from or not email_to or not email_password:
        logger.warning("Email env vars missing; would have sent weekly report to %s", email_to or "(missing)")
        logger.info("Weekly report subject: %s", subject)
        logger.info("Weekly report HTML:\n%s", html_body)
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = email_from
    message["To"] = email_to
    message.set_content("Your weekly Bourse market report is available in HTML format.")
    message.add_alternative(html_body, subtype="html")

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls()
        smtp.login(email_from, email_password)
        smtp.send_message(message)
