"""Postgres-backed report data loading for the API."""

import collections
from typing import Any

from bourse.ingest import PRICE_MIN, PRICE_MAX
from bourse.report import _detect_category
from bourse.scrape_tasks import pg_read

_ACTIVE_SQL = f"""
    WITH latest AS (
        SELECT DISTINCT ON (listing_id) listing_id, price_sek, likes
          FROM listing_snapshots ORDER BY listing_id, scraped_at DESC
    ),
    earliest AS (
        SELECT DISTINCT ON (listing_id) listing_id, likes, scraped_at AS first_scraped_at
          FROM listing_snapshots ORDER BY listing_id, scraped_at ASC
    )
    SELECT l.listing_id, l.title, l.platform, l.url,
           COALESCE(l.brand, '—') AS brand,
           lt.price_sek, COALESCE(lt.likes, 0) AS likes,
           (EXTRACT(EPOCH FROM NOW() - l.first_seen) / 86400)::INTEGER AS days_on_market,
           COALESCE(lt.likes, 0) - COALESCE(e.likes, 0) AS likes_gained,
           (EXTRACT(EPOCH FROM NOW() - e.first_scraped_at) / 86400)::FLOAT AS days_tracked
      FROM listings l
      JOIN latest lt ON lt.listing_id = l.listing_id
      JOIN earliest e ON e.listing_id = l.listing_id
     WHERE l.status = 'active' AND lt.price_sek BETWEEN {PRICE_MIN} AND {PRICE_MAX}
"""

_BRANDS_SQL = """
    SELECT brand,
           COUNT(*) AS total_listings,
           SUM(CASE WHEN status IN ('sold','removed') THEN 1 ELSE 0 END) AS sold_listings,
           AVG(CASE WHEN status IN ('sold','removed')
               THEN EXTRACT(EPOCH FROM last_seen - first_seen) / 86400
               ELSE NULL END)::FLOAT AS avg_dom_sold
      FROM listings
     WHERE brand IS NOT NULL AND TRIM(brand) != ''
     GROUP BY brand HAVING COUNT(*) >= 5
     ORDER BY SUM(CASE WHEN status IN ('sold','removed') THEN 1 ELSE 0 END)::FLOAT / COUNT(*) DESC
"""

_DAYS_SQL = """
    SELECT EXTRACT(DOW FROM COALESCE(posted_at, first_seen))::INTEGER AS day_num,
           COUNT(*) AS total,
           SUM(CASE WHEN status IN ('sold','removed') THEN 1 ELSE 0 END) AS sold_count,
           AVG(CASE WHEN status IN ('sold','removed')
               THEN EXTRACT(EPOCH FROM last_seen - first_seen) / 86400
               ELSE NULL END)::FLOAT AS avg_dom
      FROM listings GROUP BY day_num ORDER BY day_num
"""

# DOW 0=Sun,1=Mon,...,6=Sat — reorder Mon-Sun for display
_DOW_NAME = {0:"Sunday",1:"Monday",2:"Tuesday",3:"Wednesday",4:"Thursday",5:"Friday",6:"Saturday"}
_MON_FIRST_ORDER = [1, 2, 3, 4, 5, 6, 0]


def load_report_data() -> dict[str, Any]:
    with pg_read() as conn:
        with conn.cursor() as cur:
            cur.execute(_ACTIVE_SQL);  active  = [dict(r) for r in cur.fetchall()]
            cur.execute(_BRANDS_SQL);  brands  = [dict(r) for r in cur.fetchall()]
            cur.execute(_DAYS_SQL);    days    = [dict(r) for r in cur.fetchall()]

    # sell_through_by_brand
    sell_through_by_brand = []
    for r in brands:
        total = r["total_listings"]
        sold = r["sold_listings"]
        sell_through_by_brand.append({
            "brand": r["brand"],
            "sell_through_pct": round(sold * 100.0 / total, 1) if total else 0.0,
            "total_listings": total,
            "sold_listings": sold,
        })

    # best_day_to_list — Mon through Sun
    days_by_num = {r["day_num"]: r for r in days}
    best_day_to_list = []
    for dow in _MON_FIRST_ORDER:
        r = days_by_num.get(dow)
        if not r:
            continue
        total = r["total"]
        sold = r["sold_count"]
        best_day_to_list.append({
            "day": _DOW_NAME[dow],
            "sell_through_pct": round(sold * 100.0 / total, 1) if total else 0.0,
            "avg_dom": round(r["avg_dom"], 1) if r["avg_dom"] is not None else None,
        })

    # avg_price_by_category — from active listings
    cat_prices: dict[str, list[int]] = collections.defaultdict(list)
    for r in active:
        cat = _detect_category(r["title"])
        cat_prices[cat].append(r["price_sek"])
    avg_price_by_category = [
        {
            "category": cat,
            "avg_price_sek": round(sum(prices) / len(prices)),
            "listing_count": len(prices),
        }
        for cat, prices in sorted(cat_prices.items(), key=lambda x: -len(x[1]))
    ]

    # like_velocity — active listings with positive likes gained
    like_velocity = []
    for r in active:
        gained = r["likes_gained"]
        if gained <= 0:
            continue
        tracked = r["days_tracked"] or 0
        like_velocity.append({
            "title": r["title"],
            "platform": r["platform"],
            "likes_gained": gained,
            "days_tracked": round(tracked, 1),
            "velocity_per_day": round(gained / tracked, 2) if tracked > 0 else None,
        })
    like_velocity.sort(key=lambda x: x["velocity_per_day"] or 0, reverse=True)

    # top_liked — top 10 active by likes
    top_liked = sorted(active, key=lambda r: r["likes"], reverse=True)[:10]
    top_liked = [
        {
            "title": r["title"],
            "platform": r["platform"],
            "price_sek": r["price_sek"],
            "likes": r["likes"],
            "days_on_market": r["days_on_market"],
            "url": r["url"],
        }
        for r in top_liked
    ]

    return {
        "sell_through_by_brand": sell_through_by_brand,
        "best_day_to_list": best_day_to_list,
        "avg_price_by_category": avg_price_by_category,
        "like_velocity": like_velocity,
        "top_liked": top_liked,
    }
