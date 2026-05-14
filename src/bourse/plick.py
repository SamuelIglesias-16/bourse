"""Plick.se scraper — search results and listing detail pages."""

import hashlib
import json
import logging
import random
import re
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from selectolax.parser import HTMLParser

from bourse.models import Listing

logger = logging.getLogger(__name__)

BASE_URL = "https://plick.se"
CACHE_DIR = Path(".cache")
CACHE_TTL_HOURS = 24

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


# ── Cache ──────────────────────────────────────────────────────────────────


def _cache_path(url: str) -> Path:
    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    return CACHE_DIR / f"{key}.html"


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


# ── HTTP ───────────────────────────────────────────────────────────────────


def _fetch(client: httpx.Client, url: str) -> str:
    """Fetch *url* with caching, rotating User-Agent and exponential backoff.

    Raises RuntimeError after three consecutive failures.
    """
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
            logger.warning("HTTP %s on %s (attempt %d)", exc.response.status_code, url, attempt)
        except httpx.RequestError as exc:
            logger.warning("Request error %s: %s (attempt %d)", url, exc, attempt)
        if attempt < 3:
            time.sleep(backoff)
            backoff *= 2

    raise RuntimeError(f"Gave up fetching {url} after 3 attempts")


# ── Parsers ────────────────────────────────────────────────────────────────


def _parse_search_page(html: str) -> list[dict]:
    """Return raw card dicts from one Plick search-results page.

    Each dict has: plick_id, href, title, brand, size, seller_name, price_sek.
    """
    tree = HTMLParser(html)
    results: list[dict] = []

    for card in tree.css("div.listing-card-component"):
        try:
            seller_span = card.css_first(
                "div.listing-card-component__header span.text-truncate"
            )
            seller_name = seller_span.text(strip=True) if seller_span else "unknown"

            # First <a> not pointing to a seller profile is the listing link
            listing_href: Optional[str] = None
            for a in card.css("a[href]"):
                h = a.attrs.get("href", "")
                if not h.startswith("/profiler/"):
                    listing_href = h
                    break
            if not listing_href:
                continue

            # Listing ID lives in the like-button div's id attribute
            like_div = card.css_first("div[id^='like_button_ad_']")
            if not like_div:
                continue
            plick_id = like_div.attrs["id"].removeprefix("like_button_ad_")

            img = card.css_first("img.listing-card-component__card-image")
            alt = img.attrs.get("alt", "") if img else ""
            # Alt format: "Title - description text…"
            title = alt.split(" - ")[0].strip() if " - " in alt else alt.strip()
            if not title:
                continue

            footer = card.css_first("div.listing-card-component__footer")
            rows = footer.css("div.row") if footer else []

            brand = size = price_sek = None
            if rows:
                brand_el = rows[0].css_first("span")
                size_el = rows[0].css_first("strong")
                brand = brand_el.text(strip=True) if brand_el else None
                size = size_el.text(strip=True) if size_el else None
            if len(rows) >= 2:
                price_el = rows[1].css_first("strong")
                if price_el:
                    raw = (
                        price_el.text(strip=True)
                        .replace("SEK", "")
                        .replace("\xa0", "")
                        .replace(" ", "")
                        .strip()
                    )
                    try:
                        price_sek = int(raw)
                    except ValueError:
                        pass

            if price_sek is None:
                continue

            results.append(
                {
                    "plick_id": plick_id,
                    "href": listing_href,
                    "title": title,
                    "brand": brand or None,
                    "size": size or None,
                    "seller_name": seller_name,
                    "price_sek": price_sek,
                }
            )
        except Exception:
            logger.exception("Error parsing listing card")

    return results


def _parse_detail_page(html: str) -> dict:
    """Extract condition, material, likes, seller_rating, and sold-flag from a listing detail page."""
    tree = HTMLParser(html)
    result: dict = {}

    ld_script = tree.css_first('script[type="application/ld+json"]')
    if ld_script:
        try:
            data = json.loads(ld_script.text())
            ar = data.get("aggregateRating", {})
            if ar:
                result["seller_rating"] = float(ar.get("ratingValue", 0)) or None
            if (p := data.get("offers", {}).get("price")):
                result["price_sek"] = int(float(str(p)))
            # JSON-LD `offers.availability` flips to OutOfStock / SoldOut when listing is marked sold
            avail = (data.get("offers", {}).get("availability") or "").lower()
            if "soldout" in avail or "outofstock" in avail:
                result["is_sold"] = True
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # Detail rows: <div><strong>Label:</strong> value</div> inside div.well
    well = tree.css_first("div.well")
    if well:
        for div in well.css("div"):
            strong = div.css_first("strong")
            if not strong:
                continue
            label = strong.text(strip=True).rstrip(":")
            value = div.text(strip=True).replace(strong.text(strip=True), "", 1).strip()
            if label == "Skick" and value:
                result["condition"] = value
            elif label == "Material" and value:
                result["material"] = value

    likes_a = tree.css_first("a[id^='likes_link_ad_']")
    if likes_a:
        m = re.search(r"(\d+)", likes_a.text(strip=True))
        result["likes"] = int(m.group(1)) if m else 0

    # Visual Såld badge on the listing page. Plick uses class fragments like
    # "sold-badge", "label-sold", or a span with text "Såld".
    if not result.get("is_sold"):
        badge = (
            tree.css_first(".sold-badge")
            or tree.css_first(".label-sold")
            or tree.css_first("[class*='sold']")
        )
        if badge and re.search(r"s[åa]ld", badge.text(strip=True), re.IGNORECASE):
            result["is_sold"] = True

    return result


# ── Public API ─────────────────────────────────────────────────────────────


def _scrape_one_page(
    client: httpx.Client,
    query: str,
    page: int,
    position_offset: int,
    now: datetime,
    include_ids: Optional[set[str]] = None,
) -> list[Listing]:
    """Scrape one Plick search page. If include_ids is given, only fetches detail
    pages for listing IDs present in that set (skipping the rest).
    """
    params = urllib.parse.urlencode({"query": query, "page": page})
    try:
        html = _fetch(client, f"{BASE_URL}/produkter?{params}")
    except RuntimeError as exc:
        logger.error("Aborting page %d: %s", page, exc)
        return []
    cards = _parse_search_page(html)
    if not cards:
        logger.info("No listings on page %d — stopping early", page)
        return []
    listings: list[Listing] = []
    for i, card in enumerate(cards):
        lid = f"plick:{card['plick_id']}"
        if include_ids is not None and lid not in include_ids:
            continue
        detail_url = f"{BASE_URL}{card['href']}"
        try:
            detail = _parse_detail_page(_fetch(client, detail_url))
        except RuntimeError:
            logger.warning("Skipping detail page for %s", detail_url)
            detail = {}
        listings.append(Listing(
            listing_id=lid, platform="plick", url=detail_url,
            title=card["title"], brand=card.get("brand"), size=card.get("size"),
            condition=detail.get("condition"), material=detail.get("material"),
            seller_name=card["seller_name"], seller_rating=detail.get("seller_rating"),
            posted_at=None, price_sek=card["price_sek"], likes=detail.get("likes"),
            views=None, position_in_search=position_offset + i + 1, scraped_at=now,
        ))
    return listings


def scrape_query(query: str, pages: int = 5) -> list[Listing]:
    """Scrape Plick search results for *query* across *pages* pages."""
    result: list[Listing] = []
    now = datetime.now()
    with httpx.Client(follow_redirects=True) as client:
        for page in range(1, pages + 1):
            batch = _scrape_one_page(client, query, page, len(result), now)
            if not batch:
                break
            result.extend(batch)
    return result


def scrape_query_new_only(query: str, known_ids: set[str], max_pages: int = 20) -> list[Listing]:
    """Scrape pages, stopping once an entire page contains only known listing IDs."""
    result: list[Listing] = []
    now = datetime.now()
    with httpx.Client(follow_redirects=True) as client:
        for page in range(1, max_pages + 1):
            batch = _scrape_one_page(client, query, page, len(result), now)
            if not batch:
                break
            fresh = [l for l in batch if l.listing_id not in known_ids]
            result.extend(fresh)
            if not fresh:
                break
    return result


def fetch_listing(url: str) -> tuple[int | None, int | None, bool, bool]:
    """Re-fetch a single Plick listing page.

    Returns ``(price_sek, likes, is_gone, is_sold)`` — bypasses cache.

    *is_sold* is True when the seller has flipped the listing to "Såld" but
    the page is still up. *is_gone* is True when the page 404s (removed by
    seller or moderation). Callers should treat sold + final price as more
    valuable than gone — it's the actual sale data.
    """
    with httpx.Client(follow_redirects=True) as client:
        client.headers["User-Agent"] = random.choice(USER_AGENTS)
        try:
            resp = client.get(url, timeout=15)
        except httpx.RequestError as exc:
            raise RuntimeError(str(exc)) from exc
        if resp.status_code == 404:
            return None, None, True, False
        resp.raise_for_status()
    _save_cache(url, resp.text); time.sleep(random.uniform(2, 4))
    d = _parse_detail_page(resp.text)
    return d.get("price_sek"), d.get("likes"), False, bool(d.get("is_sold"))
