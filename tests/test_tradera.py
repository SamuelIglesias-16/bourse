import json
from datetime import datetime

from bourse.tradera import (
    _parse_brand_from_title,
    _parse_detail_page,
    _parse_search_page,
    _parse_size_from_title,
    fetch_listing,
)


def _search_html(items: list[dict], page_count: int = 3) -> str:
    payload = {
        "props": {
            "pageProps": {
                "initialState": {
                    "discover": {
                        "items": items,
                        "pagination": {"pageCount": page_count},
                    }
                }
            }
        }
    }
    return (
        "<html><body>"
        f"<script id='__NEXT_DATA__' type='application/json'>{json.dumps(payload)}</script>"
        "</body></html>"
    )


def _detail_html(
    *,
    item_details: dict | None = None,
    bid_info: dict | None = None,
) -> str:
    payload = {
        "props": {
            "pageProps": {
                "initialState": {
                    "views": {
                        "viewItem": {
                            "itemDetails": item_details or {},
                            "bidInfo": bid_info or {},
                        }
                    }
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
                "itemId": 123,
                "price": 1800,
                "shortDescription": "Acne Studios mock boots",
                "itemUrl": "https://www.tradera.com/item/340303/123/acne-studios-mock-boots",
                "itemType": "Auction",
                "totalBids": 4,
                "sellerAlias": "samuel",
                "startDate": "2026-05-03T21:10:13.0080000Z",
                "attributes": [
                    {"name": "shoe_size", "values": ["41"]},
                    {"name": "brand", "values": ["Acne Studios"]},
                    {"name": "condition", "values": ["Mycket gott skick"]},
                ],
            }
        ]
    )

    listings, page_count = _parse_search_page(
        html,
        position_offset=0,
        now=datetime(2026, 5, 7, 12, 0, 0),
    )

    assert page_count == 3
    assert len(listings) == 1
    listing = listings[0]
    assert listing.listing_id == "tradera:123"
    assert listing.platform == "tradera"
    assert listing.title == "Acne Studios mock boots"
    assert listing.brand == "acne studios"
    assert listing.size == "41"
    assert listing.condition == "Mycket gott skick"
    assert listing.likes == 4
    assert listing.seller_name == "samuel"
    assert listing.posted_at is not None


def test_parse_brand_from_title_uses_known_variants() -> None:
    assert _parse_brand_from_title("Maison Martin Margiela boots") == "maison margiela"
    assert _parse_brand_from_title("Rare MM6 cardigan") == "mm6"
    assert _parse_brand_from_title("Vintage wool blazer") is None


def test_parse_size_from_title_falls_back_conservatively() -> None:
    assert _parse_size_from_title("Acne jeans storlek 36/8") == "S"
    assert _parse_size_from_title("Acne jeans storlek 36") == "36"
    assert _parse_size_from_title("Acne Studios loafers 41") == "41"
    assert _parse_size_from_title("MM6 dress size one size") == "ONE SIZE"
    assert _parse_size_from_title("Vintage trench coat") is None


def test_parse_detail_page_extracts_current_price_and_bid_count() -> None:
    html = _detail_html(
        item_details={
            "itemId": 729915729,
            "title": "Acne Studios",
            "openingBid": 350,
            "buyNowPrice": None,
            "hasEnded": False,
            "isActive": True,
            "isFinalized": False,
            "forciblyClosed": False,
        },
        bid_info={
            "leadingBidAmount": 350,
            "bidCount": 1,
        },
    )

    assert _parse_detail_page(html) == (350, 1)


def test_parse_detail_page_returns_none_for_ended_listing() -> None:
    html = _detail_html(
        item_details={
            "itemId": 729915729,
            "openingBid": 350,
            "hasEnded": True,
            "isActive": False,
            "isFinalized": True,
            "forciblyClosed": False,
        },
        bid_info={"leadingBidAmount": 350, "bidCount": 4},
    )

    assert _parse_detail_page(html) == (None, None)


def test_fetch_listing_bypasses_cache_and_parses_html(monkeypatch) -> None:
    html = _detail_html(
        item_details={
            "itemId": 729980753,
            "openingBid": 360,
            "buyNowPrice": None,
            "hasEnded": False,
            "isActive": True,
            "isFinalized": False,
            "forciblyClosed": False,
        },
        bid_info={"leadingBidAmount": 0, "bidCount": 0},
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

    monkeypatch.setattr("bourse.tradera.httpx.Client", FakeClient)
    monkeypatch.setattr("bourse.tradera.time.sleep", lambda *_: None)

    assert fetch_listing("tradera:729980753", "https://www.tradera.com/item/340303/729980753") == (360, 0)
    assert requested == ["https://www.tradera.com/item/340303/729980753"]


def test_fetch_listing_returns_none_when_gone(monkeypatch) -> None:
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

    monkeypatch.setattr("bourse.tradera.httpx.Client", FakeClient)

    assert fetch_listing("tradera:729980753", "https://www.tradera.com/item/340303/729980753") == (None, None)
