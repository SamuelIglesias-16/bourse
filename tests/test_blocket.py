import json
from datetime import datetime

import pytest

from bourse.blocket import (
    _find_listings_array,
    _parse_detail_page,
    _parse_item,
    _parse_search_page,
    fetch_listing,
    scrape_query,
)


def _search_html(items: list[dict]) -> str:
    payload = {
        "props": {
            "pageProps": {
                "initialReduxState": {
                    "search": {"data": {"data": items}}
                }
            }
        }
    }
    return (
        "<html><body>"
        f"<script id='__NEXT_DATA__' type='application/json'>{json.dumps(payload)}</script>"
        "</body></html>"
    )


def _detail_html(*, ad: dict | None = None) -> str:
    payload = {
        "props": {
            "pageProps": {
                "initialReduxState": {
                    "ad": {"data": ad or {}}
                }
            }
        }
    }
    return (
        "<html><body>"
        f"<script id='__NEXT_DATA__' type='application/json'>{json.dumps(payload)}</script>"
        "</body></html>"
    )


def test_parse_search_page_extracts_listing_fields() -> None:
    html = _search_html(
        [
            {
                "ad_id": "987654321",
                "subject": "Apple iPhone 14 Pro 256GB",
                "price": {"value": 8500, "suffix": "kr"},
                "share_url": "https://www.blocket.se/annons/hela_sverige/iphone-14-pro/987654321",
                "category": {"name": "Mobiltelefoner", "slug": "elektronik/mobiltelefoner"},
                "location": {"name": "Stockholm"},
                "advertiser": {"name": "Erik"},
                "images": [{"url": "https://i.blocket.se/iphone.jpg"}],
                "list_time": "2026-05-10T12:00:00Z",
            }
        ]
    )

    listings = _parse_search_page(html, position_offset=0, now=datetime(2026, 5, 12, 12, 0, 0))
    assert len(listings) == 1

    listing = listings[0]
    assert listing.listing_id == "blocket:987654321"
    assert listing.platform == "blocket"
    assert listing.title == "Apple iPhone 14 Pro 256GB"
    assert listing.price_sek == 8500
    assert listing.url.endswith("/987654321")
    assert listing.seller_name == "Erik"
    assert listing.brand == "apple"
    assert listing.likes is None  # Blocket does not expose likes
    assert listing.image_url == "https://i.blocket.se/iphone.jpg"
    assert listing.raw_extras is not None
    assert listing.raw_extras["location"] == "Stockholm"


def test_parse_item_drops_bud_listings() -> None:
    now = datetime(2026, 5, 12, 12, 0, 0)
    listing = _parse_item(
        {
            "ad_id": "1",
            "subject": "Skicka bud — iPhone 12",
            "price": {"value": 0, "suffix": "bid"},
            "share_url": "https://www.blocket.se/annons/1",
            "category": {"name": "Mobiltelefoner"},
        },
        1,
        now,
    )
    assert listing is None


def test_parse_item_drops_zero_price() -> None:
    now = datetime(2026, 5, 12, 12, 0, 0)
    listing = _parse_item(
        {
            "ad_id": "2",
            "subject": "iPhone 12 — gratis",
            "price": {"value": 0, "suffix": "kr"},
            "share_url": "https://www.blocket.se/annons/2",
            "category": {"name": "Mobiltelefoner"},
        },
        1,
        now,
    )
    assert listing is None


def test_parse_item_drops_excluded_categories() -> None:
    now = datetime(2026, 5, 12, 12, 0, 0)
    listing = _parse_item(
        {
            "ad_id": "3",
            "subject": "Volvo V70",
            "price": {"value": 45000, "suffix": "kr"},
            "share_url": "https://www.blocket.se/annons/3",
            "category": {"name": "Bilar", "slug": "bilar"},
        },
        1,
        now,
    )
    assert listing is None


def test_parse_item_keeps_allowed_category_fashion() -> None:
    now = datetime(2026, 5, 12, 12, 0, 0)
    listing = _parse_item(
        {
            "ad_id": "4",
            "subject": "Acne Studios jeans",
            "price": {"value": 800, "suffix": "kr"},
            "share_url": "https://www.blocket.se/annons/4",
            "category": {"name": "Mode & Skönhet", "slug": "mode_och_skonhet"},
        },
        1,
        now,
    )
    assert listing is not None
    assert listing.brand == "acne studios"


def test_parse_search_page_resilient_to_alternative_shape() -> None:
    """Defensive: payload may use dehydratedState instead of initialReduxState."""
    payload = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [
                        {
                            "state": {
                                "data": {
                                    "data": [
                                        {
                                            "id": "alt-1",
                                            "subject": "PlayStation 5",
                                            "price": {"value": 4200, "suffix": "kr"},
                                            "share_url": "https://www.blocket.se/annons/alt-1",
                                            "category": {"name": "TV/Spel"},
                                        }
                                    ]
                                }
                            }
                        }
                    ]
                }
            }
        }
    }
    items = _find_listings_array(payload)
    assert len(items) == 1
    assert items[0]["id"] == "alt-1"


def test_parse_detail_page_extracts_price() -> None:
    html = _detail_html(
        ad={
            "ad_id": "12345",
            "subject": "Apple Watch Ultra",
            "price": {"value": 6800, "suffix": "kr"},
            "ad_status": "active",
        }
    )
    assert _parse_detail_page(html) == (6800, None)


def test_parse_detail_page_returns_none_for_ended_listing() -> None:
    html = _detail_html(
        ad={
            "ad_id": "12345",
            "subject": "Apple Watch Ultra",
            "price": {"value": 6800, "suffix": "kr"},
            "ad_status": "ended",
        }
    )
    assert _parse_detail_page(html) == (None, None)


def test_fetch_listing_bypasses_cache_and_returns_price(monkeypatch) -> None:
    html = _detail_html(
        ad={
            "ad_id": "999",
            "subject": "Macbook Air M2",
            "price": {"value": 9500, "suffix": "kr"},
            "ad_status": "active",
        }
    )
    requested: list[str] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.headers = kwargs.get("headers", {}).copy()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def get(self, url: str, timeout: int) -> FakeResponse:
            requested.append(url)
            return FakeResponse(html)

    monkeypatch.setattr("bourse.blocket.httpx.Client", FakeClient)
    monkeypatch.setattr("bourse.blocket.time.sleep", lambda *_: None)

    assert fetch_listing("blocket:999", "https://www.blocket.se/annons/999") == (9500, None)
    assert requested == ["https://www.blocket.se/annons/999"]


def test_fetch_listing_returns_none_when_404(monkeypatch) -> None:
    class FakeResponse:
        status_code = 404
        text = ""

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.headers = kwargs.get("headers", {}).copy()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def get(self, url: str, timeout: int) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("bourse.blocket.httpx.Client", FakeClient)
    assert fetch_listing("blocket:999", "https://www.blocket.se/annons/999") == (None, None)


def test_scrape_query_handles_multi_page(monkeypatch) -> None:
    urls: list[str] = []

    page1 = _search_html(
        [
            {
                "ad_id": "1",
                "subject": "AirPods Pro 2",
                "price": {"value": 1900, "suffix": "kr"},
                "share_url": "https://www.blocket.se/annons/1",
                "category": {"name": "Elektronik"},
                "advertiser": {"name": "anna"},
            }
        ]
    )
    page2 = _search_html(
        [
            {
                "ad_id": "2",
                "subject": "AirPods Max",
                "price": {"value": 3200, "suffix": "kr"},
                "share_url": "https://www.blocket.se/annons/2",
                "category": {"name": "Elektronik"},
                "advertiser": {"name": "bjorn"},
            }
        ]
    )

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.headers = kwargs.get("headers", {}).copy()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    def fake_fetch(client, url: str) -> str:
        urls.append(url)
        return page2 if "page=2" in url else page1

    monkeypatch.setattr("bourse.blocket.httpx.Client", FakeClient)
    monkeypatch.setattr("bourse.blocket._fetch", fake_fetch)

    listings = scrape_query("airpods", pages=2)
    assert [l.listing_id for l in listings] == ["blocket:1", "blocket:2"]
    assert urls == [
        "https://www.blocket.se/annonser/hela_sverige?q=airpods&page=1",
        "https://www.blocket.se/annonser/hela_sverige?q=airpods&page=2",
    ]
