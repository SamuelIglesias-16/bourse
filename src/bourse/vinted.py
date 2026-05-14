"""Vinted.se scraper — JSON API with anonymous cookie auth.

Uses curl-cffi to impersonate Chrome's TLS fingerprint, which is required
to pass Cloudflare Bot Management on the API endpoint.
"""

import hashlib
import json
import logging
import random
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from curl_cffi import requests as cf_requests

from bourse.models import Listing

logger = logging.getLogger(__name__)

BASE_URL = "https://www.vinted.se"
CACHE_DIR = Path(".cache")
CACHE_TTL_HOURS = 24
PER_PAGE = 24

# curl-cffi impersonation target — must match an available Chrome version
CF_IMPERSONATE = "chrome124"


# ── Cache ──────────────────────────────────────────────────────────────────


def _cache_path(url: str) -> Path:
    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    return CACHE_DIR / f"{key}.json"


def _load_cache(url: str) -> Optional[dict]:
    path = _cache_path(url)
    if not path.exists():
        return None
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    if age > timedelta(hours=CACHE_TTL_HOURS):
        return None
    logger.debug("cache hit: %s", url)
    return json.loads(path.read_text(encoding="utf-8"))


def _save_cache(url: str, data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    _cache_path(url).write_text(json.dumps(data), encoding="utf-8")


# ── HTTP ───────────────────────────────────────────────────────────────────


def _authenticate(session: cf_requests.Session) -> None:
    """Visit Vinted homepage to receive an anonymous access_token_web cookie."""
    try:
        session.get(BASE_URL, timeout=15)
        logger.debug("Vinted auth: obtained anonymous session cookie")
    except Exception as exc:
        logger.warning("Vinted auth request failed: %s", exc)


def _fetch_json(session: cf_requests.Session, url: str) -> dict:
    """Fetch a Vinted API URL as JSON with caching and exponential backoff.

    Raises RuntimeError after three consecutive failures.
    """
    cached = _load_cache(url)
    if cached is not None:
        return cached

    backoff = 2.0
    for attempt in range(1, 4):
        try:
            resp = session.get(
                url,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": BASE_URL + "/",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            _save_cache(url, data)
            time.sleep(random.uniform(1, 2))
            return data
        except cf_requests.exceptions.HTTPError as exc:
            logger.warning("HTTP %s on %s (attempt %d)", exc.response.status_code, url, attempt)
        except Exception as exc:
            logger.warning("Request error %s: %s (attempt %d)", url, exc, attempt)
        if attempt < 3:
            time.sleep(backoff)
            backoff *= 2

    raise RuntimeError(f"Gave up fetching {url} after 3 attempts")


# ── Parsers ────────────────────────────────────────────────────────────────


def _parse_item(item: dict, position: int, now: datetime) -> Optional[Listing]:
    """Convert a single Vinted API item dict into a Listing. Returns None on bad data."""
    try:
        item_id = item["id"]
        title = item.get("title", "").strip()
        if not title:
            return None

        raw_price = item.get("price", {})
        price_str = raw_price.get("amount") if isinstance(raw_price, dict) else str(raw_price)
        try:
            price_sek = int(float(price_str))
        except (TypeError, ValueError):
            return None

        url = item.get("url", f"{BASE_URL}/items/{item_id}")

        seller = item.get("user", {})
        seller_name = seller.get("login", "unknown")

        brand = item.get("brand_title") or None
        size = item.get("size_title") or None
        condition = item.get("status") or None

        likes = item.get("favourite_count")
        if likes is not None:
            likes = int(likes)

        # Vinted exposes the original listing date via the photo timestamp
        posted_at: Optional[datetime] = None
        photo = item.get("photo") or {}
        ts = (photo.get("high_resolution") or {}).get("timestamp")
        if ts:
            try:
                posted_at = datetime.fromtimestamp(int(ts))
            except (TypeError, ValueError, OSError):
                pass

        # Image: prefer high-res URL when present, fall back to the standard URL field
        image_url = (
            (photo.get("high_resolution") or {}).get("url")
            or photo.get("url")
            or photo.get("full_size_url")
        )

        return Listing(
            listing_id=f"vinted:{item_id}",
            platform="vinted",
            url=url,
            title=title,
            brand=brand,
            size=size,
            condition=condition,
            material=None,
            seller_name=seller_name,
            seller_rating=None,
            posted_at=posted_at,
            price_sek=price_sek,
            likes=likes,
            views=None,
            position_in_search=position,
            scraped_at=now,
            image_url=image_url,
        )
    except Exception:
        logger.exception("Error parsing Vinted item id=%s", item.get("id"))
        return None


# ── Public API ─────────────────────────────────────────────────────────────


def scrape_query(query: str, pages: int = 5) -> list[Listing]:
    """Scrape Vinted search results for *query* across *pages* pages."""
    now = datetime.now()
    listings: list[Listing] = []

    with cf_requests.Session(impersonate=CF_IMPERSONATE) as session:
        _authenticate(session)
        time.sleep(random.uniform(1, 2))

        position = 0
        for page in range(1, pages + 1):
            params = urllib.parse.urlencode(
                {"search_text": query, "page": page, "per_page": PER_PAGE, "order": "relevance"}
            )
            api_url = f"{BASE_URL}/api/v2/catalog/items?{params}"
            logger.info("Fetching Vinted page %d for %r", page, query)

            try:
                data = _fetch_json(session, api_url)
            except RuntimeError as exc:
                logger.error("Aborting: %s", exc)
                break

            items = data.get("items", [])
            if not items:
                logger.info("No items on page %d — stopping early", page)
                break

            for item in items:
                position += 1
                listing = _parse_item(item, position, now)
                if listing:
                    listings.append(listing)

            total_pages = data.get("pagination", {}).get("total_pages", pages)
            if page >= total_pages:
                break

    return listings


def scrape_query_new_only(query: str, known_ids: set[str], max_pages: int = 20) -> list[Listing]:
    """Scrape pages, stopping once an entire page contains only known listing IDs."""
    result: list[Listing] = []
    now = datetime.now()
    with cf_requests.Session(impersonate=CF_IMPERSONATE) as session:
        _authenticate(session)
        time.sleep(random.uniform(1, 2))
        position = 0
        for page in range(1, max_pages + 1):
            params = urllib.parse.urlencode(
                {"search_text": query, "page": page, "per_page": PER_PAGE, "order": "relevance"}
            )
            try:
                data = _fetch_json(session, f"{BASE_URL}/api/v2/catalog/items?{params}")
            except RuntimeError:
                break
            items = data.get("items", [])
            if not items:
                break
            fresh = []
            for item in items:
                position += 1
                listing = _parse_item(item, position, now)
                if listing and listing.listing_id not in known_ids:
                    fresh.append(listing)
            result.extend(fresh)
            if not fresh:
                break
    return result


def fetch_listing(listing_id: str) -> tuple[int | None, int | None, bool, bool]:
    """Re-fetch a single Vinted item via the API.

    Returns ``(price_sek, likes, is_gone, is_sold)``.

    Vinted handles disappearance two ways:
    - **410 Gone** → typically a sold item (Vinted's most common signal)
    - **404 Not Found** → seller deleted or moderation removed
    - **200 OK** with a non-active status field → sold while page is still up

    For 410 we return ``is_sold=True`` with no price; the caller estimates
    the sold price from the last known snapshot. For 404 we return
    ``is_gone=True``.
    """
    item_id = listing_id.removeprefix("vinted:")
    url = f"{BASE_URL}/api/v2/items/{item_id}"
    with cf_requests.Session(impersonate=CF_IMPERSONATE) as session:
        _authenticate(session)
        time.sleep(random.uniform(1, 2))
        try:
            resp = session.get(
                url,
                headers={"Accept": "application/json", "Referer": BASE_URL + "/"},
                timeout=15,
            )
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
        if resp.status_code == 404:
            return None, None, True, False
        if resp.status_code == 410:
            return None, None, False, True
        resp.raise_for_status()
        data = resp.json()
    item = data.get("item", {})
    if not item:
        # Empty payload usually means the item is gone — treat as sold
        # (more useful default than 'removed' given Vinted's ratio).
        return None, None, False, True

    # Status sniff: Vinted exposes the active/sold state under several keys
    # depending on API version. Check the common ones defensively.
    is_sold = False
    if item.get("is_closed") is True or item.get("is_sold") is True:
        is_sold = True
    status_id = item.get("status_id")
    if isinstance(status_id, int) and status_id in (6, 7):
        # Empirically: 6 = sold, 7 = closed
        is_sold = True
    if item.get("is_visible") is False or item.get("can_buy") is False:
        is_sold = True

    raw = item.get("price", {})
    price_str = raw.get("amount") if isinstance(raw, dict) else str(raw)
    try:
        price_sek = int(float(price_str))
    except (TypeError, ValueError):
        price_sek = None
    likes = item.get("favourite_count")
    return (
        price_sek,
        int(likes) if likes is not None else None,
        False,
        is_sold,
    )
