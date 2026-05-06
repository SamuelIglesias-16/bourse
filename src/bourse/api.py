"""FastAPI backend for Bourse."""

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

import psycopg2.extras
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from bourse.ingest import PRICE_MIN, PRICE_MAX
from bourse.report import _detect_category
from bourse.pg_report import load_report_data
from bourse.scrape_tasks import pg_read, run_scrape_new, run_scrape_update, ensure_watchlist_runs_table

load_dotenv()  # no-op on Railway (env vars already set); picks up .env locally
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

if not os.environ.get("DATABASE_URL"):
    raise RuntimeError(
        "DATABASE_URL is not set. "
        "Set it as an environment variable or add it to a .env file in the project root."
    )

ensure_watchlist_runs_table()

WATCHLIST_FILE = Path("watchlist.txt")

app = FastAPI(title="Bourse API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── watchlist ────────────────────────────────────────────────────────────────

def _read_watchlist() -> list[str]:
    if not WATCHLIST_FILE.exists():
        return []
    return [
        line.strip()
        for line in WATCHLIST_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _write_watchlist(queries: list[str]) -> None:
    WATCHLIST_FILE.write_text("\n".join(queries) + ("\n" if queries else ""), encoding="utf-8")


@app.get("/watchlist")
def get_watchlist() -> list[dict[str, Any]]:
    queries = _read_watchlist()
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
    queries = _read_watchlist()
    if item.query in queries:
        raise HTTPException(409, "Query already in watchlist")
    queries.append(item.query)
    _write_watchlist(queries)
    return {"query": item.query, "total": len(queries)}


@app.delete("/watchlist/{item}")
def remove_watchlist_item(item: str) -> dict[str, Any]:
    queries = _read_watchlist()
    if item not in queries:
        raise HTTPException(404, "Query not found in watchlist")
    queries.remove(item)
    _write_watchlist(queries)
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

    if category:
        rows = [r for r in rows if r["category"] == category]

    return rows


def _fetch_keyword_listings(q: str) -> list[dict[str, Any]]:
    keywords = [kw for kw in q.split() if kw]
    if not keywords:
        return []
    or_clauses = " OR ".join(
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
         WHERE l.status = 'active' AND ({or_clauses})
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
    return rows


@app.get("/listings/by-query")
def get_listings_by_query(q: str) -> list[dict[str, Any]]:
    return _fetch_keyword_listings(q)


@app.get("/listings/by-query/stats")
def get_listings_by_query_stats(q: str) -> dict[str, Any]:
    rows = [r for r in _fetch_keyword_listings(q) if PRICE_MIN <= r["price_sek"] <= PRICE_MAX]
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


# ── report ───────────────────────────────────────────────────────────────────

@app.get("/report")
def get_report() -> dict[str, Any]:
    return load_report_data()
