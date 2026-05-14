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
from bourse.compare import compute_arbitrage_signal, confidence_tier
from bourse.ingest import PRICE_MIN, PRICE_MAX
from bourse.report import _detect_category
from bourse.pg_report import load_report_data
from bourse.scrape_tasks import (
    pg_read,
    run_scrape_new,
    run_scrape_update,
    run_scrape_sold,
    refresh_opportunities,
    ensure_watchlist_runs_table,
    ensure_watchlist_table,
    ensure_listing_columns,
    ensure_opportunities_table,
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
    ensure_listing_columns()
    ensure_opportunities_table()
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


@app.post("/scrape/sold", status_code=202)
def trigger_scrape_sold(bg: BackgroundTasks) -> dict[str, str]:
    bg.add_task(run_scrape_sold)
    return {"status": "accepted", "message": "Sold-price scrape started — Tradera ended auctions + eBay LH_Sold"}


PLATFORM_BASE_URLS = {
    "plick": "https://www.plick.se",
    "vinted": "https://www.vinted.se",
    "tradera": "https://www.tradera.com",
    "blocket": "https://www.blocket.se",
    "ebay": "https://www.ebay.com",
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
    status: str = "active",  # active | sold | all
) -> list[dict[str, Any]]:
    status = (status or "active").lower()
    if status not in {"active", "sold", "all"}:
        raise HTTPException(400, "status must be one of: active, sold, all")
    conds: list[str] = []
    if status == "active":
        conds.append("l.status = 'active'")
    elif status == "sold":
        conds.append("l.status = 'sold'")
    # status="all" → no filter
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

    where_clause = " AND ".join(conds) if conds else "TRUE"
    order_clause = "l.last_seen DESC" if status == "sold" else "l.first_seen DESC"
    sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (listing_id) listing_id, price_sek, likes
              FROM listing_snapshots ORDER BY listing_id, scraped_at DESC
        )
        SELECT l.listing_id, l.platform, l.url, l.title,
               COALESCE(l.brand, '—') AS brand, l.size, l.condition,
               lt.price_sek, COALESCE(lt.likes, 0) AS likes, l.status,
               (EXTRACT(EPOCH FROM NOW() - l.first_seen) / 86400)::INTEGER AS days_on_market,
               l.first_seen, l.last_seen, l.image_url
          FROM listings l JOIN latest lt ON lt.listing_id = l.listing_id
         WHERE {where_clause}
         ORDER BY {order_clause}
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
        r["sold_quality"] = _sold_quality(r) if r.get("status") == "sold" else None

    if category:
        rows = [r for r in rows if r["category"] == category]

    return rows


def _sold_quality(row: dict[str, Any]) -> str:
    """Classify how reliable a sold price is for this row.

    'confirmed': real transaction price (Tradera winning bid, eBay LH_Sold).
    'estimated': asking price at time of sale (Plick Såld — no bidding/record).
    """
    platform = (row.get("platform") or "").lower()
    if platform in ("tradera", "ebay"):
        return "confirmed"
    if platform == "plick":
        return "estimated"
    return "estimated"


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
def get_platforms_compare(
    q: str,
    condition: Optional[str] = None,
    size: Optional[str] = None,
    brand: Optional[str] = None,
    days: int = 90,
) -> dict[str, Any]:
    keywords = [kw for kw in q.split() if kw]
    if not keywords:
        raise HTTPException(400, "q parameter required and must not be empty")
    if days <= 0:
        raise HTTPException(400, "days must be positive")

    joiner = " AND " if len(keywords) >= 3 else " OR "
    keyword_clauses = joiner.join(
        "(l.title ILIKE %s OR COALESCE(l.brand, '') ILIKE %s)" for _ in keywords
    )

    filter_clauses = [
        "l.status = 'active'",
        f"lt.price_sek BETWEEN {PRICE_MIN} AND {PRICE_MAX}",
        f"l.first_seen >= NOW() - INTERVAL '{int(days)} days'",
        f"({keyword_clauses})",
    ]
    base_params: list[Any] = []
    for kw in keywords:
        pat = f"%{kw}%"
        base_params.extend([pat, pat])

    if condition:
        filter_clauses.append("LOWER(COALESCE(l.condition, '')) = %s")
        base_params.append(condition.lower())
    if size:
        filter_clauses.append("LOWER(COALESCE(l.size, '')) = %s")
        base_params.append(size.lower())
    if brand:
        filter_clauses.append("LOWER(COALESCE(l.brand, '')) = %s")
        base_params.append(brand.lower())

    where_sql = " AND ".join(filter_clauses)

    aggregates_sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (listing_id) listing_id, price_sek, likes, scraped_at
              FROM listing_snapshots ORDER BY listing_id, scraped_at DESC
        )
        SELECT
            l.platform,
            COUNT(*) AS count,
            ROUND(AVG(lt.price_sek))::INTEGER AS avg_price,
            (PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY lt.price_sek))::INTEGER AS median_price,
            (PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY lt.price_sek))::INTEGER AS p25,
            (PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY lt.price_sek))::INTEGER AS p75,
            MIN(lt.price_sek) AS min_price,
            MAX(lt.price_sek) AS max_price,
            AVG(NULLIF(lt.likes, 0))::REAL AS avg_likes,
            AVG(EXTRACT(EPOCH FROM (NOW() - l.first_seen)) / 86400)::REAL AS avg_days_on_market
          FROM listings l
          JOIN latest lt ON lt.listing_id = l.listing_id
         WHERE {where_sql}
         GROUP BY l.platform
         ORDER BY count DESC
    """

    cheapest_sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (listing_id) listing_id, price_sek, likes
              FROM listing_snapshots ORDER BY listing_id, scraped_at DESC
        ), ranked AS (
            SELECT
                l.platform, l.listing_id, l.title, l.url, l.image_url,
                l.condition, l.size,
                lt.price_sek, lt.likes,
                ROW_NUMBER() OVER (PARTITION BY l.platform ORDER BY lt.price_sek ASC) AS rn
              FROM listings l
              JOIN latest lt ON lt.listing_id = l.listing_id
             WHERE {where_sql}
        )
        SELECT platform, listing_id, title, url, image_url, condition, size, price_sek, likes
          FROM ranked
         WHERE rn <= 3
         ORDER BY platform, rn
    """

    # Sold-side aggregates: median of the most recent snapshot per *sold* listing,
    # last 90 days. Independent from the active aggregates above so listing
    # counts and sold counts don't shadow each other.
    sold_clauses = [
        "l.status = 'sold'",
        f"lt.price_sek BETWEEN {PRICE_MIN} AND {PRICE_MAX}",
        "l.last_seen >= NOW() - INTERVAL '90 days'",
        f"({keyword_clauses})",
    ]
    sold_params: list[Any] = []
    for kw in keywords:
        pat = f"%{kw}%"
        sold_params.extend([pat, pat])
    if condition:
        sold_clauses.append("LOWER(COALESCE(l.condition, '')) = %s")
        sold_params.append(condition.lower())
    if size:
        sold_clauses.append("LOWER(COALESCE(l.size, '')) = %s")
        sold_params.append(size.lower())
    if brand:
        sold_clauses.append("LOWER(COALESCE(l.brand, '')) = %s")
        sold_params.append(brand.lower())
    sold_where = " AND ".join(sold_clauses)

    sold_sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (listing_id) listing_id, price_sek
              FROM listing_snapshots ORDER BY listing_id, scraped_at DESC
        )
        SELECT
            l.platform,
            COUNT(*) AS sold_count,
            (PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY lt.price_sek))::INTEGER AS median_sold_price
          FROM listings l
          JOIN latest lt ON lt.listing_id = l.listing_id
         WHERE {sold_where}
         GROUP BY l.platform
    """

    try:
        with pg_read() as conn:
            with conn.cursor() as cur:
                cur.execute(aggregates_sql, base_params)
                aggregate_rows = [dict(r) for r in cur.fetchall()]
                cur.execute(cheapest_sql, base_params)
                cheap_rows = [dict(r) for r in cur.fetchall()]
                cur.execute(sold_sql, sold_params)
                sold_rows = {r["platform"]: dict(r) for r in cur.fetchall()}
    except Exception:
        logger.exception("GET /platforms/compare query failed")
        raise HTTPException(500, "Database query failed — check server logs")

    cheapest_by_platform: dict[str, list[dict[str, Any]]] = {}
    for row in cheap_rows:
        cheapest_by_platform.setdefault(row["platform"], []).append(
            {
                "id": row["listing_id"],
                "title": row["title"],
                "url": row["url"] or PLATFORM_BASE_URLS.get(row["platform"], ""),
                "image_url": row["image_url"],
                "condition": row["condition"],
                "size": row["size"],
                "price_sek": row["price_sek"],
                "likes": row["likes"],
            }
        )

    platforms: list[dict[str, Any]] = []
    seen_platforms: set[str] = set()
    for row in aggregate_rows:
        avg_likes = row["avg_likes"]
        avg_dom = row["avg_days_on_market"]
        sold = sold_rows.get(row["platform"], {})
        platforms.append(
            {
                "platform": row["platform"],
                "count": int(row["count"]),
                "avg_price": int(row["avg_price"]) if row["avg_price"] is not None else None,
                "median_price": int(row["median_price"]) if row["median_price"] is not None else None,
                "p25": int(row["p25"]) if row["p25"] is not None else None,
                "p75": int(row["p75"]) if row["p75"] is not None else None,
                "min_price": int(row["min_price"]) if row["min_price"] is not None else None,
                "max_price": int(row["max_price"]) if row["max_price"] is not None else None,
                "avg_likes": round(float(avg_likes), 1) if avg_likes is not None else None,
                "avg_days_on_market": round(float(avg_dom), 1) if avg_dom is not None else None,
                "sample_confidence": confidence_tier(int(row["count"])),
                "sold_count": int(sold.get("sold_count") or 0),
                "median_sold_price": int(sold["median_sold_price"]) if sold.get("median_sold_price") is not None else None,
                "cheapest_3": cheapest_by_platform.get(row["platform"], []),
            }
        )
        seen_platforms.add(row["platform"])

    # Platforms with only sold data (no current active listings) still belong in the response.
    for platform_name, sold in sold_rows.items():
        if platform_name in seen_platforms:
            continue
        platforms.append(
            {
                "platform": platform_name,
                "count": 0,
                "avg_price": None, "median_price": None,
                "p25": None, "p75": None,
                "min_price": None, "max_price": None,
                "avg_likes": None, "avg_days_on_market": None,
                "sample_confidence": "low",
                "sold_count": int(sold["sold_count"] or 0),
                "median_sold_price": int(sold["median_sold_price"]) if sold.get("median_sold_price") is not None else None,
                "cheapest_3": [],
            }
        )

    return {
        "query": q,
        "filters": {"condition": condition, "size": size, "brand": brand, "days": days},
        "total_listings": sum(p["count"] for p in platforms),
        "platforms": platforms,
        "arbitrage_signal": compute_arbitrage_signal(platforms),
    }


@app.get("/platforms/compare/history")
def get_platforms_compare_history(q: str, days: int = 30) -> dict[str, Any]:
    """Per-platform daily median prices over the last *days* days."""
    keywords = [kw for kw in q.split() if kw]
    if not keywords:
        raise HTTPException(400, "q parameter required and must not be empty")
    if days <= 0 or days > 365:
        raise HTTPException(400, "days must be in 1..365")

    joiner = " AND " if len(keywords) >= 3 else " OR "
    keyword_clauses = joiner.join(
        "(l.title ILIKE %s OR COALESCE(l.brand, '') ILIKE %s)" for _ in keywords
    )
    params: list[Any] = []
    for kw in keywords:
        pat = f"%{kw}%"
        params.extend([pat, pat])

    sql = f"""
        SELECT
            l.platform,
            DATE_TRUNC('day', s.scraped_at)::DATE AS day,
            (PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY s.price_sek))::INTEGER AS median_price,
            COUNT(*) AS sample_size
          FROM listings l
          JOIN listing_snapshots s ON s.listing_id = l.listing_id
         WHERE s.price_sek BETWEEN {PRICE_MIN} AND {PRICE_MAX}
           AND s.scraped_at >= NOW() - INTERVAL '{int(days)} days'
           AND ({keyword_clauses})
         GROUP BY l.platform, day
         ORDER BY l.platform, day
    """

    try:
        with pg_read() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("GET /platforms/compare/history query failed")
        raise HTTPException(500, "Database query failed — check server logs")

    by_platform: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_platform.setdefault(row["platform"], []).append(
            {
                "day": row["day"].isoformat() if row["day"] else None,
                "median_price": int(row["median_price"]),
                "sample_size": int(row["sample_size"]),
            }
        )

    return {
        "query": q,
        "days": days,
        "platforms": [
            {"platform": platform, "points": points}
            for platform, points in by_platform.items()
        ],
    }


@app.get("/platforms/opportunities")
def get_platforms_opportunities(
    limit: int = 20,
    min_margin_pct: Optional[float] = None,
) -> dict[str, Any]:
    """Top arbitrage opportunities across the watchlist, sorted by est_margin_sek desc.

    The underlying table is refreshed automatically after every scrape; trigger a
    manual recompute with POST /platforms/opportunities/refresh.
    """
    if limit <= 0 or limit > 200:
        raise HTTPException(400, "limit must be in 1..200")

    sql = "SELECT * FROM arbitrage_opportunities"
    conds: list[str] = []
    params: list[Any] = []
    if min_margin_pct is not None:
        conds.append("est_margin_pct >= %s")
        params.append(min_margin_pct)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY est_margin_sek DESC LIMIT %s"
    params.append(limit)

    try:
        with pg_read() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
                cur.execute("SELECT MAX(computed_at) AS latest FROM arbitrage_opportunities")
                latest_row = cur.fetchone()
    except Exception:
        logger.exception("GET /platforms/opportunities query failed")
        raise HTTPException(500, "Database query failed — check server logs")

    latest = latest_row["latest"] if latest_row else None
    return {
        "computed_at": latest.isoformat() if latest else None,
        "count": len(rows),
        "opportunities": [
            {
                "query": r["query"],
                "buy_platform": r["buy_platform"],
                "sell_platform": r["sell_platform"],
                "buy_median_sek": int(r["buy_median_sek"]),
                "sell_median_sek": int(r["sell_median_sek"]),
                "est_margin_sek": int(r["est_margin_sek"]),
                "est_margin_pct": float(r["est_margin_pct"]) if r["est_margin_pct"] is not None else None,
                "gross_margin_sek": int(r["gross_margin_sek"]),
                "total_listings": int(r["total_listings"]),
                "computed_at": r["computed_at"].isoformat() if r["computed_at"] else None,
            }
            for r in rows
        ],
    }


@app.post("/platforms/opportunities/refresh", status_code=202)
def trigger_opportunities_refresh(bg: BackgroundTasks) -> dict[str, str]:
    """Recompute the arbitrage_opportunities table without re-scraping."""
    bg.add_task(refresh_opportunities)
    return {"status": "accepted", "message": "Opportunities recompute started in background"}


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
