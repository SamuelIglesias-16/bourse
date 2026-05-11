"""FastAPI backend for Bourse."""

import logging
import os
import subprocess
from typing import Any, Optional

import psycopg2.extras
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from bourse.alert_routes import router as alerts_router
from bourse.ingest import PRICE_MIN, PRICE_MAX
from bourse.report import _detect_category
from bourse.pg_report import load_report_data
from bourse.scrape_tasks import (
    pg_read,
    run_scrape_new,
    run_scrape_update,
    ensure_watchlist_runs_table,
    ensure_watchlist_table,
    pg_read_watchlist,
    pg_watchlist_add,
    pg_watchlist_remove,
)

load_dotenv()  # no-op on Railway (env vars already set); picks up .env locally
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

if not os.environ.get("DATABASE_URL"):
    raise RuntimeError(
        "DATABASE_URL is not set. "
        "Set it as an environment variable or add it to a .env file in the project root."
    )

try:
    ensure_watchlist_runs_table()
    ensure_watchlist_table()
except Exception:
    logger.warning("Startup: could not ensure watchlist tables — DB may not be ready yet")

app = FastAPI(title="Bourse API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(alerts_router)


# ── watchlist ────────────────────────────────────────────────────────────────

@app.get("/watchlist")
def get_watchlist() -> list[dict[str, Any]]:
    queries = pg_read_watchlist()
    if not queries:
        return []
    try:
        with pg_read() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT query, last_run_at, listings_found FROM watchlist_runs WHERE query = ANY(%s)",
                    (queries,),
                )
                runs = {r["query"]: dict(r) for r in cur.fetchall()}
    except Exception:
        logger.warning("GET /watchlist: could not load run stats")
        runs = {}
    return [
        {
            "query": q,
            "last_scraped_at": runs[q]["last_run_at"].isoformat() if q in runs else None,
            "listings_found": runs[q]["listings_found"] if q in runs else None,
        }
        for q in queries
    ]


class WatchlistItem(BaseModel):
    query: str


@app.post("/watchlist", status_code=201)
def add_watchlist_item(item: WatchlistItem) -> dict[str, Any]:
    inserted = pg_watchlist_add(item.query)
    if not inserted:
        raise HTTPException(409, "Query already in watchlist")
    queries = pg_read_watchlist()
    return {"query": item.query, "total": len(queries)}


@app.delete("/watchlist/{item}")
def remove_watchlist_item(item: str) -> dict[str, Any]:
    deleted = pg_watchlist_remove(item)
    if not deleted:
        raise HTTPException(404, "Query not found in watchlist")
    queries = pg_read_watchlist()
    return {"removed": item, "remaining": len(queries)}


# ── scrape ───────────────────────────────────────────────────────────────────

@app.post("/scrape", status_code=202)
def trigger_scrape(bg: BackgroundTasks) -> dict[str, str]:
    bg.add_task(subprocess.run, ["bourse", "scrape", "all", "--watchlist"], check=False)
    return {"status": "accepted", "message": "Full scrape started in background"}


@app.post("/scrape/new", status_code=202)
def trigger_scrape_new(bg: BackgroundTasks) -> dict[str, str]:
    bg.add_task(run_scrape_new)
    return {"status": "accepted", "message": "Scrape-new job started — stops when no new listings found on a page"}


@app.post("/scrape/update", status_code=202)
def trigger_scrape_update(bg: BackgroundTasks) -> dict[str, str]:
    bg.add_task(run_scrape_update)
    return {"status": "accepted", "message": "Scrape-update job started — rechecks existing active listings"}


PLATFORM_BASE_URLS = {
    "plick": "https://www.plick.se",
    "vinted": "https://www.vinted.se",
    "tradera": "https://www.tradera.com",
}


# ── listings ─────────────────────────────────────────────────────────────────

@app.get("/listings")
def get_listings(
    platform: Optional[str] = None,
    brand: Optional[str] = None,
    category: Optional[str] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    sane_prices: bool = False,
) -> list[dict[str, Any]]:
    conds = ["l.status = 'active'"]
    params: list[Any] = []
    if platform:
        conds.append("l.platform = %s")
        params.append(platform)
    if brand:
        conds.append("l.brand = %s")
        params.append(brand.lower())
    if min_price is not None:
        conds.append("lt.price_sek >= %s")
        params.append(min_price)
    if max_price is not None:
        conds.append("lt.price_sek <= %s")
        params.append(max_price)
    if sane_prices:
        conds.append(f"lt.price_sek BETWEEN {PRICE_MIN} AND {PRICE_MAX}")

    sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (listing_id) listing_id, price_sek, likes
              FROM listing_snapshots ORDER BY listing_id, scraped_at DESC
        )
        SELECT l.listing_id, l.platform, l.url, l.title,
               COALESCE(l.brand, '—') AS brand, l.size, l.condition,
               lt.price_sek, COALESCE(lt.likes, 0) AS likes, l.status,
               (EXTRACT(EPOCH FROM NOW() - l.first_seen) / 86400)::INTEGER AS days_on_market,
               l.first_seen, l.last_seen
          FROM listings l JOIN latest lt ON lt.listing_id = l.listing_id
         WHERE {" AND ".join(conds)}
         ORDER BY l.first_seen DESC
    """
    try:
        with pg_read() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("GET /listings query failed")
        raise HTTPException(500, "Database query failed — check server logs")

    for r in rows:
        r["category"] = _detect_category(r["title"])
        r["first_seen"] = r["first_seen"].isoformat() if r["first_seen"] else None
        r["last_seen"] = r["last_seen"].isoformat() if r["last_seen"] else None
        if not r.get("url"):
            r["url"] = PLATFORM_BASE_URLS.get(r["platform"], "")

    if category:
        rows = [r for r in rows if r["category"] == category]

    return rows


def _fetch_keyword_listings(q: str) -> list[dict[str, Any]]:
    keywords = [kw for kw in q.split() if kw]
    if not keywords:
        return []
    # 3+ words = require ALL (precise: "maison margiela gats" shouldn't return every margiela);
    # 1-2 words = ANY (broader recall, e.g. "acne studios").
    joiner = " AND " if len(keywords) >= 3 else " OR "
    keyword_clauses = joiner.join(
        "(l.title ILIKE %s OR COALESCE(l.brand, '') ILIKE %s)" for _ in keywords
    )
    params: list[Any] = []
    for kw in keywords:
        pat = f"%{kw}%"
        params.extend([pat, pat])
    sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (listing_id) listing_id, price_sek, likes
              FROM listing_snapshots ORDER BY listing_id, scraped_at DESC
        )
        SELECT l.listing_id, l.platform, l.url, l.title,
               COALESCE(l.brand, '—') AS brand, l.size, l.condition,
               lt.price_sek, COALESCE(lt.likes, 0) AS likes, l.status,
               (EXTRACT(EPOCH FROM NOW() - l.first_seen) / 86400)::INTEGER AS days_on_market,
               l.first_seen, l.last_seen
          FROM listings l JOIN latest lt ON lt.listing_id = l.listing_id
         WHERE l.status = 'active' AND ({keyword_clauses})
         ORDER BY l.first_seen DESC
    """
    try:
        with pg_read() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("GET /listings/by-query query failed")
        raise HTTPException(500, "Database query failed — check server logs")
    for r in rows:
        r["category"] = _detect_category(r["title"])
        r["first_seen"] = r["first_seen"].isoformat() if r["first_seen"] else None
        r["last_seen"] = r["last_seen"].isoformat() if r["last_seen"] else None
        if not r.get("url"):
            r["url"] = PLATFORM_BASE_URLS.get(r["platform"], "")
    return rows


@app.get("/listings/by-query")
def get_listings_by_query(q: str) -> list[dict[str, Any]]:
    return _fetch_keyword_listings(q)


@app.get("/listings/by-query/stats")
def get_listings_by_query_stats(q: str) -> dict[str, Any]:
    # Tighter ceiling than PRICE_MAX (50_000): bud/joke listings and rare luxury outliers
    # skew the avg/max for stats. 15_000 covers virtually all real secondhand fashion.
    rows = [r for r in _fetch_keyword_listings(q) if PRICE_MIN <= r["price_sek"] <= 15_000]
    if not rows:
        return {
            "total_listings": 0,
            "avg_price": None, "min_price": None, "max_price": None,
            "avg_likes": None,
            "top_liked_title": None, "top_liked_price": None,
            "top_liked_likes": None, "top_liked_url": None,
        }
    prices = [r["price_sek"] for r in rows]
    likes_list = [r["likes"] for r in rows]
    top = max(rows, key=lambda r: r["likes"])
    return {
        "total_listings": len(rows),
        "avg_price": round(sum(prices) / len(prices)),
        "min_price": min(prices),
        "max_price": max(prices),
        "avg_likes": round(sum(likes_list) / len(likes_list), 1),
        "top_liked_title": top["title"],
        "top_liked_price": top["price_sek"],
        "top_liked_likes": top["likes"],
        "top_liked_url": top["url"],
    }


@app.get("/listings/{listing_id}")
def get_listing(listing_id: str) -> dict[str, Any]:
    # Registered AFTER /listings/by-query* so those literal paths match first.
    sql = """
        WITH latest AS (
            SELECT DISTINCT ON (listing_id) listing_id, price_sek, likes
              FROM listing_snapshots
             WHERE listing_id = %s
             ORDER BY listing_id, scraped_at DESC
        )
        SELECT l.listing_id, l.platform, l.url, l.title,
               COALESCE(l.brand, '—') AS brand, l.size, l.condition,
               lt.price_sek, COALESCE(lt.likes, 0) AS likes, l.status,
               (EXTRACT(EPOCH FROM NOW() - l.first_seen) / 86400)::INTEGER AS days_on_market,
               l.first_seen, l.last_seen
          FROM listings l JOIN latest lt ON lt.listing_id = l.listing_id
         WHERE l.listing_id = %s
    """
    try:
        with pg_read() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (listing_id, listing_id))
                row = cur.fetchone()
    except Exception:
        logger.exception("GET /listings/{listing_id} query failed")
        raise HTTPException(500, "Database query failed — check server logs")
    if not row:
        raise HTTPException(404, "Listing not found")
    r = dict(row)
    r["category"] = _detect_category(r["title"])
    r["first_seen"] = r["first_seen"].isoformat() if r["first_seen"] else None
    r["last_seen"] = r["last_seen"].isoformat() if r["last_seen"] else None
    if not r.get("url"):
        r["url"] = PLATFORM_BASE_URLS.get(r["platform"], "")
    return r


@app.get("/listings/{listing_id}/history")
def get_listing_history(listing_id: str) -> list[dict[str, Any]]:
    with pg_read() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM listing_snapshots WHERE listing_id = %s ORDER BY scraped_at ASC",
                (listing_id,),
            )
            rows = cur.fetchall()
    if not rows:
        raise HTTPException(404, "Listing not found or has no snapshots")
    result = []
    for r in rows:
        d = dict(r)
        d["scraped_at"] = d["scraped_at"].isoformat() if d["scraped_at"] else None
        result.append(d)
    return result


# ── stats ────────────────────────────────────────────────────────────────────

@app.get("/stats")
def get_stats() -> dict[str, Any]:
    sql = """
        WITH first_prices AS (
            SELECT DISTINCT ON (listing_id) listing_id, price_sek
              FROM listing_snapshots ORDER BY listing_id, scraped_at ASC
        ),
        latest_prices AS (
            SELECT DISTINCT ON (listing_id) listing_id, price_sek
              FROM listing_snapshots ORDER BY listing_id, scraped_at DESC
        )
        SELECT
            (SELECT COUNT(*) FROM listings) AS total_listings,
            (SELECT COUNT(*) FROM listings WHERE first_seen >= CURRENT_DATE) AS new_today,
            (SELECT COUNT(*)
               FROM listings l
               JOIN first_prices f  ON f.listing_id  = l.listing_id
               JOIN latest_prices lp ON lp.listing_id = l.listing_id
              WHERE l.status = 'active' AND lp.price_sek < f.price_sek
            ) AS active_price_drops,
            (SELECT MAX(last_run_at) FROM watchlist_runs) AS last_scraped_at,
            (SELECT CASE WHEN COUNT(*) > 0
                         THEN ROUND(100.0 * SUM(CASE WHEN status = 'sold' THEN 1 ELSE 0 END) / COUNT(*), 1)
                         ELSE 0.0 END
               FROM listings) AS avg_sell_through_pct
    """
    try:
        with pg_read() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                row = dict(cur.fetchone())
    except Exception:
        logger.exception("GET /stats query failed")
        return {
            "total_listings": 0,
            "new_today": 0,
            "active_price_drops": 0,
            "avg_sell_through_pct": 0.0,
            "last_scraped_at": None,
        }
    last_scraped_at = row["last_scraped_at"]
    return {
        "total_listings": row["total_listings"] or 0,
        "new_today": row["new_today"] or 0,
        "active_price_drops": row["active_price_drops"] or 0,
        "avg_sell_through_pct": float(row["avg_sell_through_pct"] or 0.0),
        "last_scraped_at": last_scraped_at.isoformat() if last_scraped_at else None,
    }


# ── platforms ────────────────────────────────────────────────────────────────

@app.get("/platforms/compare")
def get_platforms_compare(q: str) -> dict[str, Any]:
    keywords = [kw for kw in q.split() if kw]
    if not keywords:
        raise HTTPException(400, "q parameter required and must not be empty")

    # Same matching rule as /listings/by-query: AND for precise multi-word queries.
    joiner = " AND " if len(keywords) >= 3 else " OR "
    keyword_clauses = joiner.join(
        "(l.title ILIKE %s OR COALESCE(l.brand, '') ILIKE %s)" for _ in keywords
    )
    params: list[Any] = []
    for kw in keywords:
        pat = f"%{kw}%"
        params.extend([pat, pat])

    sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (listing_id) listing_id, price_sek
              FROM listing_snapshots ORDER BY listing_id, scraped_at DESC
        )
        SELECT
            l.platform,
            COUNT(*) AS count,
            ROUND(AVG(lt.price_sek))::INTEGER AS avg_price,
            (PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY lt.price_sek))::INTEGER AS median_price,
            MIN(lt.price_sek) AS min_price,
            MAX(lt.price_sek) AS max_price
          FROM listings l
          JOIN latest lt ON lt.listing_id = l.listing_id
         WHERE l.status = 'active'
           AND lt.price_sek BETWEEN {PRICE_MIN} AND {PRICE_MAX}
           AND ({keyword_clauses})
         GROUP BY l.platform
        HAVING COUNT(*) >= 3
         ORDER BY count DESC
    """
    try:
        with pg_read() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("GET /platforms/compare query failed")
        raise HTTPException(500, "Database query failed — check server logs")

    return {
        "query": q,
        "total_listings": sum(r["count"] for r in rows),
        "platforms": rows,
    }


# ── report ───────────────────────────────────────────────────────────────────

@app.get("/report")
def get_report() -> dict[str, Any]:
    try:
        return load_report_data()
    except Exception:
        logger.exception("GET /report failed — returning empty report")
        return {
            "sell_through_by_brand": [],
            "best_day_to_list": [],
            "avg_price_by_category": [],
            "like_velocity": [],
            "top_liked": [],
        }
