"""eBay.com scraper — international price reference for arbitrage.

eBay's search results page is server-rendered HTML with a stable structure:
each result is an ``<li class="s-item">`` (or ``<div>``) containing
``.s-item__title``, ``.s-item__price``, ``.s-item__shipping`` and a link.
We parse this HTML rather than hitting the official API to avoid auth.

Two flows:
- ``scrape_query(query)`` → active listings (``_sop=15`` = price+ship ascending)
- ``scrape_query_ended(query)`` → completed sales (``LH_Sold=1&LH_Complete=1``)

eBay prices listings in USD; we convert once at ingest using
``fees.USD_TO_SEK``. Shipping cost (where exposed) is captured separately and
fed into the arbitrage signal so cross-border deals are honestly priced.

If httpx hits Cloudflare/bot-protection (403), we fall back to curl-cffi
Chrome impersonation — same strategy as vinted.py and blocket.py.
"""

from __future__ import annotations

import hashlib
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

from bourse.fees import EBAY_SHIPPING_TO_SE_SEK, USD_TO_SEK
from bourse.models import Listing
from bourse.normalize import normalize_brand

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ebay.com"
CACHE_DIR = Path(".cache")
CACHE_TTL_HOURS = 24

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# Sanity bands for eBay (in USD before conversion). Items below $5 are usually
# accessories; over $5000 are luxury watches/cars we shouldn't be flipping.
EBAY_PRICE_MIN_USD = 5
EBAY_PRICE_MAX_USD = 5000


# ── Cache ────────────────────────────────────────────────────────────────────


def _cache_path(url: str) -> Path:
    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    return CACHE_DIR / f"ebay-{key}.html"


def _load_cache(url: str) -> Optional[str]:
    path = _cache_path(url)
    if not path.exists():
        return None
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    if age > timedelta(hours=CACHE_TTL_HOURS):
        return None
    return path.read_text(encoding="utf-8")


def _save_cache(url: str, html: str) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    _cache_path(url).write_text(html, encoding="utf-8")


# ── HTTP ─────────────────────────────────────────────────────────────────────


def _fetch_via_curl_cffi(url: str) -> str:
    from curl_cffi import requests as cf_requests

    with cf_requests.Session(impersonate="chrome124") as session:
        resp = session.get(
            url,
            headers={"Accept-Language": "en-US,en;q=0.9", "Referer": BASE_URL + "/"},
            timeout=20,
        )
        if resp.status_code in (404, 410):
            return ""
        resp.raise_for_status()
        return resp.text


def _fetch(client: httpx.Client, url: str) -> str:
    cached = _load_cache(url)
    if cached:
        return cached

    backoff = 2.0
    for attempt in range(1, 4):
        client.headers["User-Agent"] = random.choice(USER_AGENTS)
        try:
            resp = client.get(url, timeout=20)
            if resp.status_code == 403:
                logger.info("eBay 403 — falling back to curl-cffi for %s", url)
                html = _fetch_via_curl_cffi(url)
                if html:
                    _save_cache(url, html)
                    time.sleep(random.uniform(3, 5))
                return html
            resp.raise_for_status()
            _save_cache(url, resp.text)
            time.sleep(random.uniform(3, 5))  # stricter than other platforms
            return resp.text
        except httpx.HTTPStatusError as exc:
            logger.warning("HTTP %s on %s (attempt %d)", exc.response.status_code, url, attempt)
        except httpx.RequestError as exc:
            logger.warning("Request error %s: %s (attempt %d)", url, exc, attempt)
        if attempt < 3:
            time.sleep(backoff)
            backoff *= 2

    raise RuntimeError(f"Gave up fetching {url} after 3 attempts")


# ── URL builders ─────────────────────────────────────────────────────────────


def _active_search_url(query: str, page: int) -> str:
    params = urllib.parse.urlencode({"_nkw": query, "_sop": 15, "_pgn": page})
    return f"{BASE_URL}/sch/i.html?{params}"


def _sold_search_url(query: str, page: int) -> str:
    params = urllib.parse.urlencode(
        {"_nkw": query, "LH_Sold": 1, "LH_Complete": 1, "_pgn": page}
    )
    return f"{BASE_URL}/sch/i.html?{params}"


# ── Parsing ──────────────────────────────────────────────────────────────────


_PRICE_RE = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)")
_ITEM_ID_RE = re.compile(r"/itm/(?:[^/]+/)?(\d{6,})")


def _usd_to_sek(usd: float) -> int:
    return int(round(usd * USD_TO_SEK))


def _parse_usd(text: str | None) -> float | None:
    if not text:
        return None
    text = text.replace("US ", "").replace(",", "")
    match = _PRICE_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_shipping_usd(text: str | None) -> float:
    """Free shipping → 0.0. Numeric value → that. Else fallback to EBAY_SHIPPING_TO_SE_SEK ÷ rate."""
    if not text:
        return EBAY_SHIPPING_TO_SE_SEK / USD_TO_SEK
    lowered = text.lower()
    if "free" in lowered:
        return 0.0
    parsed = _parse_usd(text)
    if parsed is None:
        return EBAY_SHIPPING_TO_SE_SEK / USD_TO_SEK
    return parsed


def _extract_item_id(href: str) -> str | None:
    if not href:
        return None
    match = _ITEM_ID_RE.search(href)
    return match.group(1) if match else None


_BRAND_PATTERNS = (
    "apple", "samsung", "sony", "bose", "nintendo", "playstation",
    "fujifilm", "canon", "nikon", "leica", "olympus", "ricoh", "contax",
    "acne studios", "maison margiela", "carhartt", "patagonia", "arcteryx",
    "stussy", "marantz", "sennheiser", "casio", "seiko", "rolex",
)


def _brand_from_title(title: str) -> Optional[str]:
    lowered = title.lower()
    for cand in _BRAND_PATTERNS:
        if re.search(rf"\b{re.escape(cand)}\b", lowered):
            return normalize_brand(cand) or None
    return None


def _parse_card(node: Any, position: int, now: datetime, *, sold: bool) -> Optional[Listing]:
    """Parse one ``.s-item`` card. Returns None on missing essentials."""
    try:
        link_el = node.css_first("a.s-item__link")
        href = link_el.attrs.get("href") if link_el else ""
        item_id = _extract_item_id(href or "")
        if not item_id:
            return None

        title_el = node.css_first(".s-item__title") or node.css_first("h3.s-item__title")
        title = title_el.text(strip=True) if title_el else ""
        # Skip eBay's first-row banner card with the placeholder title
        if not title or title.lower().startswith("shop on ebay"):
            return None

        price_el = node.css_first(".s-item__price")
        price_usd = _parse_usd(price_el.text(strip=True) if price_el else None)
        if price_usd is None:
            return None
        if price_usd < EBAY_PRICE_MIN_USD or price_usd > EBAY_PRICE_MAX_USD:
            return None

        shipping_el = node.css_first(".s-item__shipping") or node.css_first(".s-item__logisticsCost")
        shipping_usd = _parse_shipping_usd(shipping_el.text(strip=True) if shipping_el else None)

        img_el = node.css_first("img.s-item__image-img") or node.css_first(".s-item__image img")
        image_url = (img_el.attrs.get("src") or img_el.attrs.get("data-src")) if img_el else None

        end_date = None
        date_el = node.css_first(".s-item__ended-date") or node.css_first(".s-item__title--tagblock__COMPLETED")
        if date_el:
            end_date = date_el.text(strip=True)

        price_sek = _usd_to_sek(price_usd)
        shipping_sek = _usd_to_sek(shipping_usd)

        return Listing(
            listing_id=f"ebay:{item_id}",
            platform="ebay",
            url=href,
            title=title,
            brand=_brand_from_title(title),
            size=None,
            condition=None,
            material=None,
            seller_name="ebay-seller",
            seller_rating=None,
            posted_at=None,
            price_sek=price_sek,
            likes=None,
            views=None,
            position_in_search=position,
            scraped_at=now,
            image_url=image_url,
            shipping_sek=shipping_sek,
            status_override="sold" if sold else None,
            raw_extras={
                "price_usd": price_usd,
                "shipping_usd": shipping_usd,
                "end_date": end_date,
            },
        )
    except Exception:
        logger.exception("Error parsing eBay card")
        return None


def _parse_search_page(html: str, position_offset: int, now: datetime, *, sold: bool) -> list[Listing]:
    tree = HTMLParser(html)
    cards = tree.css(".s-item") or tree.css("li.s-item")
    listings: list[Listing] = []
    for i, card in enumerate(cards):
        listing = _parse_card(card, position_offset + i + 1, now, sold=sold)
        if listing:
            listings.append(listing)
    return listings


def _parse_detail_page(html: str) -> tuple[int | None, int | None]:
    """Returns (price_sek, likes_or_None). likes are not exposed on eBay."""
    tree = HTMLParser(html)
    price_el = (
        tree.css_first(".x-price-primary span")
        or tree.css_first("[data-testid='x-price-primary']")
        or tree.css_first("#prcIsum")
    )
    usd = _parse_usd(price_el.text(strip=True) if price_el else None)
    if usd is None:
        return None, None
    if usd < EBAY_PRICE_MIN_USD or usd > EBAY_PRICE_MAX_USD:
        return None, None
    return _usd_to_sek(usd), None


# ── Public API ───────────────────────────────────────────────────────────────


def scrape_query(query: str, pages: int = 3) -> list[Listing]:
    """Scrape active eBay listings for *query* (default 3 pages)."""
    now = datetime.now()
    listings: list[Listing] = []
    with httpx.Client(follow_redirects=True, headers={"Accept-Language": "en-US,en;q=0.9"}) as client:
        for page in range(1, pages + 1):
            try:
                html = _fetch(client, _active_search_url(query, page))
            except RuntimeError as exc:
                logger.error("Aborting eBay active scrape: %s", exc)
                break
            if not html:
                break
            page_listings = _parse_search_page(html, position_offset=len(listings), now=now, sold=False)
            if not page_listings:
                break
            listings.extend(page_listings)
    return listings


def scrape_query_ended(query: str, pages: int = 3) -> list[Listing]:
    """Scrape completed/sold eBay listings for *query*."""
    now = datetime.now()
    listings: list[Listing] = []
    with httpx.Client(follow_redirects=True, headers={"Accept-Language": "en-US,en;q=0.9"}) as client:
        for page in range(1, pages + 1):
            try:
                html = _fetch(client, _sold_search_url(query, page))
            except RuntimeError as exc:
                logger.error("Aborting eBay sold scrape: %s", exc)
                break
            if not html:
                break
            page_listings = _parse_search_page(html, position_offset=len(listings), now=now, sold=True)
            if not page_listings:
                break
            listings.extend(page_listings)
    return listings


def scrape_query_new_only(query: str, known_ids: set[str], max_pages: int = 5) -> list[Listing]:
    """Scrape active pages until we hit a page with no new IDs."""
    result: list[Listing] = []
    now = datetime.now()
    with httpx.Client(follow_redirects=True, headers={"Accept-Language": "en-US,en;q=0.9"}) as client:
        for page in range(1, max_pages + 1):
            try:
                html = _fetch(client, _active_search_url(query, page))
            except RuntimeError:
                break
            if not html:
                break
            page_listings = _parse_search_page(html, position_offset=(page - 1) * 60, now=now, sold=False)
            if not page_listings:
                break
            fresh = [l for l in page_listings if l.listing_id not in known_ids]
            result.extend(fresh)
            if not fresh:
                break
    return result


def fetch_listing(listing_id: str, url: str) -> tuple[int | None, int | None]:
    """Re-fetch an eBay listing detail page. Returns (price_sek, likes_or_None)."""
    with httpx.Client(
        follow_redirects=True, headers={"Accept-Language": "en-US,en;q=0.9"}
    ) as client:
        client.headers["User-Agent"] = random.choice(USER_AGENTS)
        try:
            resp = client.get(url, timeout=20)
        except httpx.RequestError as exc:
            raise RuntimeError(str(exc)) from exc
        if resp.status_code in (404, 410):
            return None, None
        if resp.status_code == 403:
            try:
                html = _fetch_via_curl_cffi(url)
            except Exception:
                return None, None
            if not html:
                return None, None
            time.sleep(random.uniform(3, 5))
            return _parse_detail_page(html)
        resp.raise_for_status()

    time.sleep(random.uniform(3, 5))
    try:
        return _parse_detail_page(resp.text)
    except Exception:
        logger.warning("Could not parse eBay detail for %s", listing_id)
        return None, None
