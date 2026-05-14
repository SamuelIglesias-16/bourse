"""Blocket.se scraper.

Blocket is a Schibsted-owned Next.js site. Search results are rendered server-side
with an embedded ``__NEXT_DATA__`` JSON blob containing the listings array.

URL shape:
    https://www.blocket.se/annonser/hela_sverige?q={query}&page={n}

Category filtering happens at the URL level by appending a category path segment:
    /annonser/hela_sverige/{category_slug}?q=...

We use the unrestricted "hela_sverige" search and filter categories in the parser.
The allowed categories below match the goal spec (electronics, fashion,
sport/outdoor, watches, photo); listings outside these categories are dropped.

If httpx returns 403 (Cloudflare bot challenge) the scraper falls back to
curl-cffi with Chrome impersonation — same fallback strategy as vinted.py.

Likes are not publicly exposed on Blocket so the Listing.likes field is left
None. seller_name + location are captured and stored in the listing's raw JSON
blob (location is not yet a first-class column).
"""

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

BASE_URL = "https://www.blocket.se"
SEARCH_PATH = "/annonser/hela_sverige"
CACHE_DIR = Path(".cache")
CACHE_TTL_HOURS = 24

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

ALLOWED_CATEGORY_KEYWORDS = (
    "elektronik",
    "mode",
    "skonhet",
    "skönhet",
    "sport",
    "fritid",
    "klockor",
    "smycken",
    "foto",
    "film",
    "kameror",
    "tv",
    "ljud",
    "video",
    "datorer",
    "spel",
    "mobiltelefoner",
    "klader",
    "kläder",
    "skor",
)

EXCLUDED_CATEGORY_KEYWORDS = (
    "bilar",
    "motor",
    "fordon",
    "motorcyklar",
    "lastbilar",
    "husvagn",
    "bostad",
    "lagenhet",
    "lägenhet",
    "fastighet",
    "tomter",
    "jobb",
    "arbete",
    "tjanster",
    "tjänster",
)

EXCLUDED_PRICE_TYPES = {"bid", "skicka_bud", "auktion", "ge_bud", "offer"}


# ── Cache ────────────────────────────────────────────────────────────────────


def _cache_path(url: str) -> Path:
    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    return CACHE_DIR / f"blocket-{key}.html"


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


# ── HTTP ─────────────────────────────────────────────────────────────────────


def _fetch_via_curl_cffi(url: str) -> str:
    """Cloudflare-resistant fetch via curl-cffi Chrome impersonation."""
    from curl_cffi import requests as cf_requests

    with cf_requests.Session(impersonate="chrome124") as session:
        resp = session.get(
            url,
            headers={
                "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
                "Referer": BASE_URL + "/",
            },
            timeout=15,
        )
        if resp.status_code in (404, 410):
            return ""
        resp.raise_for_status()
        return resp.text


def _fetch(client: httpx.Client, url: str) -> str:
    """Fetch *url* with caching. Falls back to curl-cffi if httpx hits Cloudflare."""
    cached = _load_cache(url)
    if cached:
        return cached

    backoff = 2.0
    last_status: int | None = None
    for attempt in range(1, 4):
        client.headers["User-Agent"] = random.choice(USER_AGENTS)
        try:
            resp = client.get(url, timeout=15)
            last_status = resp.status_code
            if resp.status_code == 403:
                logger.info("Blocket returned 403 — falling back to curl-cffi for %s", url)
                html = _fetch_via_curl_cffi(url)
                if html:
                    _save_cache(url, html)
                    time.sleep(random.uniform(2, 4))
                return html
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

    raise RuntimeError(f"Gave up fetching {url} after 3 attempts (last status {last_status})")


def _search_url(query: str, page: int) -> str:
    params = urllib.parse.urlencode({"q": query, "page": str(page)})
    return f"{BASE_URL}{SEARCH_PATH}?{params}"


# ── Next.js payload extraction ───────────────────────────────────────────────


def _extract_next_data(html: str) -> dict[str, Any]:
    tree = HTMLParser(html)
    script = tree.css_first("script#__NEXT_DATA__")
    if not script:
        raise ValueError("Missing __NEXT_DATA__ script")
    return json.loads(script.text())


def _find_listings_array(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Locate the listings array within Next.js payload (defensive against shape drift)."""
    page_props = payload.get("props", {}).get("pageProps", {})

    candidates: list[Any] = []
    initial = page_props.get("initialReduxState") or {}
    candidates.append(initial.get("search", {}).get("data"))
    candidates.append(initial.get("listings", {}).get("data"))
    candidates.append(page_props.get("data"))
    candidates.append(page_props.get("listings"))
    candidates.append(page_props.get("searchData"))

    dehydrated = page_props.get("dehydratedState") or {}
    for query in dehydrated.get("queries", []) or []:
        state = query.get("state") or {}
        candidates.append(state.get("data"))

    for candidate in candidates:
        items = _extract_items(candidate)
        if items:
            return items

    return []


def _extract_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (dict, list)):
        return []
    if isinstance(value, list):
        # already a list of dicts?
        if value and isinstance(value[0], dict):
            return value
        return []
    for key in ("data", "ads", "items", "results", "listings", "hits"):
        sub = value.get(key)
        if isinstance(sub, list) and sub and isinstance(sub[0], dict):
            return sub
    return []


# ── Item parsing ─────────────────────────────────────────────────────────────


def _allowed_category(category: str | None) -> bool:
    if not category:
        return True  # be permissive when category metadata is missing
    lowered = category.lower()
    if any(bad in lowered for bad in EXCLUDED_CATEGORY_KEYWORDS):
        return False
    if any(good in lowered for good in ALLOWED_CATEGORY_KEYWORDS):
        return True
    return False


def _extract_price(item: dict[str, Any]) -> tuple[int | None, str | None]:
    raw = item.get("price")
    price_type: str | None = None
    amount: Any = None
    if isinstance(raw, dict):
        price_type = (raw.get("suffix") or raw.get("type") or "").lower() or None
        amount = raw.get("value") if raw.get("value") is not None else raw.get("amount")
    else:
        amount = raw
    if isinstance(amount, str):
        amount = re.sub(r"[^\d]", "", amount) or None
    try:
        return (int(float(amount)) if amount not in (None, "") else None), price_type
    except (TypeError, ValueError):
        return None, price_type


def _extract_category(item: dict[str, Any]) -> str | None:
    cat = item.get("category")
    if isinstance(cat, dict):
        return cat.get("name") or cat.get("label") or cat.get("slug")
    if isinstance(cat, list) and cat:
        first = cat[0]
        if isinstance(first, dict):
            return first.get("name") or first.get("label") or first.get("slug")
        if isinstance(first, str):
            return first
    if isinstance(cat, str):
        return cat
    return item.get("category_name") or item.get("category_slug")


def _extract_image(item: dict[str, Any]) -> str | None:
    images = item.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            return first.get("url") or first.get("src")
        if isinstance(first, str):
            return first
    image = item.get("image")
    if isinstance(image, dict):
        return image.get("url") or image.get("src")
    if isinstance(image, str):
        return image
    return None


def _extract_location(item: dict[str, Any]) -> str | None:
    loc = item.get("location")
    if isinstance(loc, dict):
        return loc.get("name") or loc.get("label")
    if isinstance(loc, list) and loc:
        first = loc[0]
        if isinstance(first, dict):
            return first.get("name") or first.get("label")
        if isinstance(first, str):
            return first
    if isinstance(loc, str):
        return loc
    return None


def _extract_url(item: dict[str, Any]) -> str | None:
    share = item.get("share_url") or item.get("shareUrl") or item.get("url")
    if isinstance(share, str) and share:
        if share.startswith("http"):
            return share
        return f"{BASE_URL}{share}" if share.startswith("/") else f"{BASE_URL}/{share}"
    return None


def _extract_seller(item: dict[str, Any]) -> str:
    advertiser = item.get("advertiser") or item.get("seller") or {}
    if isinstance(advertiser, dict):
        return (
            advertiser.get("name")
            or advertiser.get("login")
            or advertiser.get("public_profile", {}).get("name")
            or "unknown"
        )
    return "unknown"


def _extract_size_from_attrs(item: dict[str, Any]) -> str | None:
    attrs = item.get("parameter_groups") or item.get("parameters")
    if not attrs:
        return None
    if isinstance(attrs, list):
        for group in attrs:
            if isinstance(group, dict):
                params = group.get("parameters") or group.get("items") or [group]
                for p in params:
                    if not isinstance(p, dict):
                        continue
                    label = (p.get("label") or p.get("name") or "").lower()
                    if "storlek" in label or "size" in label:
                        value = p.get("value") or p.get("values")
                        if isinstance(value, list) and value:
                            return str(value[0])
                        if isinstance(value, str):
                            return value
    return None


def _parse_item(item: dict[str, Any], position: int, now: datetime) -> Optional[Listing]:
    try:
        item_id = item.get("ad_id") or item.get("id") or item.get("listId")
        if item_id is None:
            return None
        item_id = str(item_id)

        title = (item.get("subject") or item.get("title") or "").strip()
        if not title:
            return None

        category = _extract_category(item)
        if not _allowed_category(category):
            return None

        price_sek, price_type = _extract_price(item)
        if price_type and price_type in EXCLUDED_PRICE_TYPES:
            return None
        if price_sek is None or price_sek == 0:
            return None

        url = _extract_url(item) or f"{BASE_URL}/annons/{item_id}"
        seller_name = _extract_seller(item)
        location = _extract_location(item)
        image_url = _extract_image(item)
        size_raw = _extract_size_from_attrs(item)

        brand = _parse_brand_from_title(title)
        size = normalize_size(size_raw) if size_raw else _parse_size_from_title(title)

        return Listing(
            listing_id=f"blocket:{item_id}",
            platform="blocket",
            url=url,
            title=title,
            brand=brand,
            size=size or None,
            condition=None,
            material=None,
            seller_name=seller_name,
            seller_rating=None,
            posted_at=_parse_posted_at(item.get("list_time") or item.get("listTime")),
            price_sek=price_sek,
            likes=None,
            views=None,
            position_in_search=position,
            scraped_at=now,
            image_url=image_url,
            raw_extras={"location": location, "category": category} if (location or category) else None,
        )
    except Exception:
        logger.exception("Error parsing Blocket item id=%s", item.get("ad_id") or item.get("id"))
        return None


TITLE_BRAND_PATTERNS = (
    "apple",
    "samsung",
    "sony",
    "bose",
    "nintendo",
    "playstation",
    "fujifilm",
    "canon",
    "nikon",
    "leica",
    "olympus",
    "ricoh",
    "contax",
    "acne studios",
    "maison margiela",
    "carhartt",
    "patagonia",
    "arcteryx",
    "stussy",
    "marantz",
    "sennheiser",
    "casio",
    "seiko",
)


def _parse_brand_from_title(title: str) -> Optional[str]:
    lowered = title.lower()
    for candidate in TITLE_BRAND_PATTERNS:
        if re.search(rf"\b{re.escape(candidate)}\b", lowered):
            return normalize_brand(candidate) or None
    return None


SIZE_RE = re.compile(
    r"(?:storlek|strl|size)\s*[:\-]?\s*"
    r"(one size|xxs|xs|s|m|l|xl|xxl|w\d{2}|\d{2}(?:/\d+)?)",
    re.IGNORECASE,
)


def _parse_size_from_title(title: str) -> Optional[str]:
    match = SIZE_RE.search(title)
    if not match:
        return None
    return normalize_size(match.group(1)) or None


def _parse_posted_at(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value))
        except (OSError, ValueError):
            return None
    if isinstance(value, str):
        cleaned = re.sub(r"\.(\d{6})\d+(?=Z|[+-])", r".\1", value)
        try:
            return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _parse_search_page(html: str, position_offset: int, now: datetime) -> list[Listing]:
    payload = _extract_next_data(html)
    items = _find_listings_array(payload)
    listings: list[Listing] = []
    for index, item in enumerate(items):
        listing = _parse_item(item, position_offset + index + 1, now)
        if listing:
            listings.append(listing)
    return listings


def _parse_detail_page(html: str) -> tuple[int | None, int | None]:
    """Parse a single ad page for current price. Returns (price_sek, likes=None).

    Blocket's detail page also embeds __NEXT_DATA__ with the full ad. We pull
    just the price; likes are not publicly exposed.
    """
    payload = _extract_next_data(html)
    page_props = payload.get("props", {}).get("pageProps", {})

    ad: dict[str, Any] | None = None
    initial = page_props.get("initialReduxState") or {}
    ad_state = initial.get("ad") or initial.get("currentAd") or {}
    ad = ad_state.get("data") if isinstance(ad_state, dict) else None
    if not ad:
        ad = page_props.get("ad") or page_props.get("data")

    dehydrated = page_props.get("dehydratedState") or {}
    if not ad:
        for query in dehydrated.get("queries", []) or []:
            state = query.get("state") or {}
            data = state.get("data") or {}
            if isinstance(data, dict) and (data.get("subject") or data.get("price")):
                ad = data
                break

    if not isinstance(ad, dict):
        return None, None

    status = (ad.get("ad_status") or ad.get("status") or "").lower()
    if status in {"ended", "removed", "deleted", "sold"}:
        return None, None

    price_sek, price_type = _extract_price(ad)
    if price_type and price_type in EXCLUDED_PRICE_TYPES:
        return None, None
    return price_sek, None


# ── Public scrape API ────────────────────────────────────────────────────────


def scrape_query(query: str, pages: int = 5) -> list[Listing]:
    """Scrape Blocket search results for *query* across *pages* pages."""
    now = datetime.now()
    listings: list[Listing] = []

    with httpx.Client(
        follow_redirects=True,
        headers={"Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8"},
    ) as client:
        for page in range(1, pages + 1):
            url = _search_url(query, page)
            logger.info("Fetching Blocket page %d for %r", page, query)
            try:
                html = _fetch(client, url)
            except RuntimeError as exc:
                logger.error("Aborting: %s", exc)
                break
            if not html:
                break

            page_listings = _parse_search_page(html, position_offset=len(listings), now=now)
            if not page_listings:
                logger.info("No Blocket listings on page %d — stopping early", page)
                break
            listings.extend(page_listings)

    return listings


def scrape_query_new_only(query: str, known_ids: set[str], max_pages: int = 10) -> list[Listing]:
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
            if not html:
                break

            page_listings = _parse_search_page(
                html, position_offset=(page - 1) * 40, now=now
            )
            if not page_listings:
                break
            fresh = [listing for listing in page_listings if listing.listing_id not in known_ids]
            result.extend(fresh)
            if not fresh:
                break
    return result


def fetch_listing(listing_id: str, url: str) -> tuple[int | None, int | None]:
    """Re-fetch a single Blocket ad page. Returns (price_sek, likes_or_None).

    likes is always None for Blocket. Returns (None, None) when the ad is gone.
    """
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
        if resp.status_code == 403:
            try:
                html = _fetch_via_curl_cffi(url)
            except Exception:
                return None, None
            if not html:
                return None, None
            time.sleep(random.uniform(2, 4))
            try:
                return _parse_detail_page(html)
            except Exception:
                logger.warning("Could not parse Blocket detail page for %s", listing_id)
                return None, None
        resp.raise_for_status()

    time.sleep(random.uniform(2, 4))
    try:
        return _parse_detail_page(resp.text)
    except Exception:
        logger.warning("Could not parse Blocket detail page for %s", listing_id)
        return None, None
