"""Unit tests for the eBay scraper."""

from __future__ import annotations

from datetime import datetime

from bourse.ebay import (
    _parse_search_page,
    _parse_card,
    _parse_detail_page,
    _parse_usd,
    _parse_shipping_usd,
    _usd_to_sek,
)
from bourse.fees import USD_TO_SEK, EBAY_SHIPPING_TO_SE_SEK
from selectolax.parser import HTMLParser


def _search_html(items: list[dict]) -> str:
    cards = ""
    for it in items:
        cards += f"""
        <li class="s-item">
          <a class="s-item__link" href="{it['href']}"></a>
          <div class="s-item__title">{it['title']}</div>
          <div class="s-item__price">{it['price']}</div>
          <div class="s-item__shipping">{it.get('shipping', 'Free shipping')}</div>
          <img class="s-item__image-img" src="{it.get('image', '')}" />
          {('<span class="s-item__ended-date">' + it['ended'] + '</span>') if it.get('ended') else ''}
        </li>
        """
    return f"<html><body><ul>{cards}</ul></body></html>"


def test_parse_usd_handles_formats() -> None:
    assert _parse_usd("$1,234.56") == 1234.56
    assert _parse_usd("US $999.00") == 999.0
    assert _parse_usd("$45 to $99") == 45.0  # picks the first number
    assert _parse_usd(None) is None
    assert _parse_usd("garbage") is None


def test_parse_shipping_usd_free_vs_numeric_vs_missing() -> None:
    assert _parse_shipping_usd("Free shipping") == 0.0
    assert _parse_shipping_usd("+$15.00 shipping") == 15.0
    # Missing shipping → use the international default in USD
    assert _parse_shipping_usd(None) == EBAY_SHIPPING_TO_SE_SEK / USD_TO_SEK


def test_usd_to_sek_uses_fees_rate() -> None:
    assert _usd_to_sek(100) == int(round(100 * USD_TO_SEK))


def test_parse_search_page_basic() -> None:
    html = _search_html(
        [
            {
                "href": "https://www.ebay.com/itm/iphone-14-pro/123456789012?epid=stuff",
                "title": "Apple iPhone 14 Pro 256GB Excellent",
                "price": "$680.00",
                "shipping": "Free shipping",
                "image": "https://i.ebayimg.com/iphone.jpg",
            }
        ]
    )
    listings = _parse_search_page(html, position_offset=0, now=datetime(2026, 5, 14), sold=False)
    assert len(listings) == 1
    l = listings[0]
    assert l.listing_id == "ebay:123456789012"
    assert l.platform == "ebay"
    assert l.price_sek == _usd_to_sek(680.0)
    assert l.shipping_sek == 0  # free shipping
    assert l.image_url == "https://i.ebayimg.com/iphone.jpg"
    assert l.brand == "apple"
    assert l.status_override is None  # active flow


def test_sold_flow_sets_status_override() -> None:
    html = _search_html(
        [
            {
                "href": "https://www.ebay.com/itm/airpods/987654321098",
                "title": "Apple AirPods Pro 2 SOLD",
                "price": "$170",
                "shipping": "+$15.00 shipping",
                "ended": "Sold May 10, 2026",
            }
        ]
    )
    listings = _parse_search_page(html, position_offset=0, now=datetime(2026, 5, 14), sold=True)
    assert len(listings) == 1
    l = listings[0]
    assert l.status_override == "sold"
    assert l.shipping_sek == _usd_to_sek(15.0)
    assert l.raw_extras["end_date"] == "Sold May 10, 2026"
    assert l.raw_extras["price_usd"] == 170.0


def test_skip_below_minimum_price() -> None:
    html = _search_html(
        [
            {
                "href": "https://www.ebay.com/itm/cheap/111111111111",
                "title": "Tiny cheap accessory",
                "price": "$2.99",
            }
        ]
    )
    listings = _parse_search_page(html, position_offset=0, now=datetime(2026, 5, 14), sold=False)
    assert listings == []


def test_skip_first_banner_card() -> None:
    """eBay always renders one banner card with a 'Shop on eBay' title — drop it."""
    html = """
    <html><body><ul>
      <li class="s-item">
        <a class="s-item__link" href="https://www.ebay.com/sch/i.html?_nkw=banner"></a>
        <div class="s-item__title">Shop on eBay</div>
        <div class="s-item__price">$20.00</div>
      </li>
    </ul></body></html>
    """
    listings = _parse_search_page(html, position_offset=0, now=datetime(2026, 5, 14), sold=False)
    assert listings == []


def test_parse_detail_page_returns_price_sek() -> None:
    html = """
    <html><body>
      <div class="x-price-primary"><span>$420.00</span></div>
    </body></html>
    """
    price, likes = _parse_detail_page(html)
    assert price == _usd_to_sek(420.0)
    assert likes is None


def test_parse_card_drops_without_item_id() -> None:
    tree = HTMLParser(
        """
        <li class="s-item">
          <a class="s-item__link" href="https://www.ebay.com/itm/no-id-here"></a>
          <div class="s-item__title">Stuff</div>
          <div class="s-item__price">$100</div>
        </li>
        """
    )
    card = tree.css_first(".s-item")
    assert _parse_card(card, 1, datetime(2026, 5, 14), sold=False) is None
