"""Tradera scraper built from embedded search-page JSON."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
from selectolax.parser import HTMLParser

from bourse.models import Listing
from bourse.normalize import normalize_brand, normalize_size

logger = logging.getLogger(__name__)

BASE_URL = "https://www.tradera.com"
CACHE_DIR = Path(".cache")
CACHE_TTL_HOURS = 24
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]
TITLE_BRAND_PATTERNS = [
    "acne studios stockholm",
    "acne studio stockholm",
    "maison martin margiela",
    "mm6 maison margiela",
    "acne studios",
    "maison margiela",
    "masion margiela",
    "maison marginal",
    "acne studio",
    "acne jeans",
    "margiela",
    "acne",
    "mm6",
]
KEYWORD_SIZE_RE = re.compile(
    r"(?:storlek|strl|size)\s*[:\-]?\s*"
    r"(one size|onesize|xxxs|xxs|xs|s|m|l|xl|xxl|xxxl|w\d{2}|\d{2}(?:/\d+)?)",
    re.IGNORECASE,
)
TRAILING_SIZE_RE = re.compile(
    r"(?:^|[\s(/-])(w\d{2}|xxxs|xxs|xs|s|m|l|xl|xxl|xxxl|3[5-9]|4[0-7])"
    r"(?=$|[\s),/-])",
    re.IGNORECASE,
)


def _cache_path(url: str) -> Path:
    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    return CACHE_DIR / f"tradera-{key}.html"


def _load_cache(url: str) -> Optional[str]:
    path = _cache_path(url)
    if not path.exists():
        return None
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    if age > timedelta(hours=CACHE_TTL_HOURS):
        return None
    logger.debug("cache hit: %s", url)
    return path.read_text(encoding="utf-8")


def _save_cache(url: str, html: str) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    _cache_path(url).write_text(html, encoding="utf-8")


def _fetch(client: httpx.Client, url: str) -> str:
    """Fetch *url* with caching, rotating User-Agent and exponential backoff."""
    cached = _load_cache(url)
    if cached:
        return cached

    backoff = 2.0
    for attempt in range(1, 4):
        client.headers["User-Agent"] = random.choice(USER_AGENTS)
        try:
            resp = client.get(url, timeout=15)
            resp.raise_for_status()
            _save_cache(url, resp.text)
            time.sleep(random.uniform(2, 4))
            return resp.text
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "HTTP %s on %s (attempt %d)",
                exc.response.status_code,
                url,
                attempt,
            )
        except httpx.RequestError as exc:
            logger.warning("Request error %s: %s (attempt %d)", url, exc, attempt)
        if attempt < 3:
            time.sleep(backoff)
            backoff *= 2

    raise RuntimeError(f"Gave up fetching {url} after 3 attempts")


def _search_url(query: str, page: int) -> str:
    params = urllib.parse.urlencode({"q": query, "paging": str(page)})
    return f"{BASE_URL}/search?{params}"


def _extract_next_data(html: str) -> dict[str, Any]:
    tree = HTMLParser(html)
    script = tree.css_first("script#__NEXT_DATA__")
    if not script:
        raise ValueError("Missing __NEXT_DATA__ script")
    return json.loads(script.text())


def _attribute_map(attributes: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for attr in attributes:
        name = attr.get("name")
        values = attr.get("values")
        if not name or not isinstance(values, list) or not values:
            continue
        value = values[0]
        if isinstance(value, str) and value.strip():
            result[name] = value.strip()
    return result


def _first_attr(attrs: dict[str, str], *names: str) -> Optional[str]:
    for name in names:
        value = attrs.get(name)
        if value:
            return value
    return None


def _parse_brand_from_title(title: str) -> Optional[str]:
    lowered = title.lower()
    for candidate in TITLE_BRAND_PATTERNS:
        if re.search(rf"\b{re.escape(candidate)}\b", lowered):
            brand = normalize_brand(candidate)
            return brand or None
    return None


def _parse_size_from_title(title: str) -> Optional[str]:
    match = KEYWORD_SIZE_RE.search(title)
    if match:
        size = normalize_size(match.group(1))
        return size or None

    matches = list(TRAILING_SIZE_RE.finditer(title))
    if not matches:
        return None
    size = normalize_size(matches[-1].group(1))
    return size or None


def _parse_posted_at(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    cleaned = re.sub(r"\.(\d{6})\d+(?=Z|[+-])", r".\1", value)
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_item(item: dict[str, Any], position: int, now: datetime) -> Optional[Listing]:
    try:
        item_id = item["itemId"]
        title = item.get("shortDescription", "").strip()
        if not title:
            return None

        price_raw = item.get("price") or item.get("buyNowPrice")
        if price_raw in (None, ""):
            return None
        price_sek = int(float(price_raw))

        attr_map = _attribute_map(item.get("attributes", []))
        brand = _first_attr(attr_map, "brand") or _parse_brand_from_title(title)
        size = _first_attr(attr_map, "clothes_size", "shoe_size") or _parse_size_from_title(title)
        condition = _first_attr(attr_map, "condition")
        likes_raw = item.get("totalBids")
        likes = int(likes_raw) if likes_raw is not None else None
        url = item.get("itemUrl", f"{BASE_URL}/item/{item_id}")
        seller_name = item.get("sellerAlias") or "unknown"

        return Listing(
            listing_id=f"tradera:{item_id}",
            platform="tradera",
            url=url,
            title=title,
            brand=brand,
            size=size,
            condition=condition,
            material=None,
            seller_name=seller_name,
            seller_rating=None,
            posted_at=_parse_posted_at(item.get("startDate")),
            price_sek=price_sek,
            likes=likes,
            views=None,
            position_in_search=position,
            scraped_at=now,
        )
    except Exception:
        logger.exception("Error parsing Tradera item id=%s", item.get("itemId"))
        return None


def _parse_search_page(html: str, position_offset: int, now: datetime) -> tuple[list[Listing], int]:
    data = _extract_next_data(html)
    discover = data["props"]["pageProps"]["initialState"]["discover"]
    items = discover.get("items", [])
    page_count = discover.get("pagination", {}).get("pageCount", 1)

    listings: list[Listing] = []
    for index, item in enumerate(items):
        listing = _parse_item(item, position_offset + index + 1, now)
        if listing:
            listings.append(listing)

    return listings, int(page_count)


def _parse_detail_page(html: str) -> tuple[int | None, int | None]:
    data = _extract_next_data(html)
    view_item = data["props"]["pageProps"]["initialState"]["views"]["viewItem"]
    item_details = view_item.get("itemDetails") or {}
    bid_info = view_item.get("bidInfo") or {}
    if not item_details:
        return None, None

    if (
        item_details.get("hasEnded")
        or item_details.get("isFinalized")
        or item_details.get("forciblyClosed")
        or not item_details.get("isActive", True)
    ):
        return None, None

    likes_raw = bid_info.get("bidCount")
    likes = int(likes_raw) if likes_raw is not None else None

    price_raw = bid_info.get("leadingBidAmount")
    if price_raw in (None, 0, "0", ""):
        price_raw = item_details.get("buyNowPrice") or item_details.get("openingBid")
    if price_raw in (None, ""):
        return None, likes

    return int(float(price_raw)), likes


def scrape_query(query: str, pages: int = 5) -> list[Listing]:
    """Scrape Tradera search results for *query* across *pages* pages."""
    now = datetime.now()
    listings: list[Listing] = []

    with httpx.Client(
        follow_redirects=True,
        headers={"Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8"},
    ) as client:
        total_pages = pages
        for page in range(1, pages + 1):
            url = _search_url(query, page)
            logger.info("Fetching Tradera page %d for %r", page, query)
            try:
                html = _fetch(client, url)
            except RuntimeError as exc:
                logger.error("Aborting: %s", exc)
                break

            page_listings, page_count = _parse_search_page(
                html,
                position_offset=len(listings),
                now=now,
            )
            if not page_listings:
                logger.info("No listings on page %d — stopping early", page)
                break

            listings.extend(page_listings)
            total_pages = min(pages, page_count)
            if page >= total_pages:
                break

    return listings


def scrape_query_new_only(query: str, known_ids: set[str], max_pages: int = 20) -> list[Listing]:
    """Scrape pages until a full page contains only already-known listing IDs."""
    result: list[Listing] = []
    now = datetime.now()

    with httpx.Client(
        follow_redirects=True,
        headers={"Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8"},
    ) as client:
        for page in range(1, max_pages + 1):
            try:
                html = _fetch(client, _search_url(query, page))
            except RuntimeError:
                break

            page_listings, _ = _parse_search_page(
                html,
                position_offset=(page - 1) * 80,
                now=now,
            )
            if not page_listings:
                break

            fresh = [listing for listing in page_listings if listing.listing_id not in known_ids]
            result.extend(fresh)
            if not fresh:
                break

    return result


def fetch_listing(listing_id: str, url: str) -> tuple[int | None, int | None]:
    """Re-fetch a single Tradera listing page. Returns (price_sek, likes)."""
    with httpx.Client(
        follow_redirects=True,
        headers={"Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8"},
    ) as client:
        client.headers["User-Agent"] = random.choice(USER_AGENTS)
        try:
            resp = client.get(url, timeout=15)
        except httpx.RequestError as exc:
            raise RuntimeError(str(exc)) from exc
        if resp.status_code in (404, 410):
            return None, None
        resp.raise_for_status()

    time.sleep(random.uniform(2, 4))
    try:
        return _parse_detail_page(resp.text)
    except Exception:
        logger.warning("Could not parse Tradera detail page for %s", listing_id)
        return None, None
