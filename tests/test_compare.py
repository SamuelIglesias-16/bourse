"""Unit tests for the /platforms/compare business logic."""

from __future__ import annotations

import pytest

from bourse.compare import compute_arbitrage_signal, confidence_tier
from bourse.fees import PLATFORM_FEES, SHIPPING_COST_SEK


def _platform(name: str, *, count: int, median: int) -> dict:
    return {"platform": name, "count": count, "median_price": median}


def test_confidence_tier_thresholds() -> None:
    assert confidence_tier(0) == "low"
    assert confidence_tier(4) == "low"
    assert confidence_tier(5) == "medium"
    assert confidence_tier(15) == "medium"
    assert confidence_tier(16) == "high"
    assert confidence_tier(1000) == "high"


def test_arbitrage_signal_picks_highest_median_spread() -> None:
    platforms = [
        _platform("plick", count=10, median=2000),    # cheapest median
        _platform("vinted", count=10, median=2500),
        _platform("tradera", count=10, median=2800),  # highest median (but 10% fee)
    ]
    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    assert sig["buy_platform"] == "plick"
    assert sig["sell_platform"] == "tradera"
    assert sig["est_margin_sek"] > 0


def test_arbitrage_signal_returns_none_when_only_one_platform_eligible() -> None:
    platforms = [
        _platform("plick", count=10, median=2000),
        _platform("vinted", count=2, median=2500),  # low sample
    ]
    assert compute_arbitrage_signal(platforms) is None


def test_arbitrage_signal_returns_none_when_all_low_sample() -> None:
    platforms = [
        _platform("plick", count=3, median=2000),
        _platform("vinted", count=4, median=2500),
    ]
    assert compute_arbitrage_signal(platforms) is None


def test_arbitrage_signal_returns_none_for_empty_input() -> None:
    assert compute_arbitrage_signal([]) is None


def test_arbitrage_signal_net_includes_fees_and_shipping() -> None:
    """Vinted sell side (5%): revenue = median × 0.95; cost = buy_median + shipping."""
    platforms = [
        _platform("plick", count=10, median=1000),
        _platform("vinted", count=10, median=1500),
    ]
    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    assert sig["buy_platform"] == "plick"
    assert sig["sell_platform"] == "vinted"
    expected_revenue = 1500 * (1.0 - PLATFORM_FEES["vinted"])
    expected_cost = 1000 + SHIPPING_COST_SEK
    expected_margin = round(expected_revenue - expected_cost)
    assert sig["est_margin_sek"] == expected_margin
    # gross omits fee + shipping
    assert sig["gross_margin_sek"] == 1500 - 1000
    assert sig["buy_median_sek"] == 1000
    assert sig["sell_median_sek"] == 1500


def test_arbitrage_signal_works_with_all_four_platforms() -> None:
    """Across 4 platforms the signal evaluates all 12 ordered pairs."""
    platforms = [
        _platform("blocket", count=20, median=1800),   # cheapest median
        _platform("plick", count=10, median=2000),
        _platform("vinted", count=10, median=2400),
        _platform("tradera", count=10, median=2800),   # highest median
    ]
    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    assert sig["buy_platform"] == "blocket"
    assert sig["sell_platform"] == "tradera"
    # revenue = 2800 × 0.90 = 2520; cost = 1800 + 79 = 1879; margin = 641
    expected_revenue = 2800 * (1.0 - PLATFORM_FEES["tradera"])
    expected_cost = 1800 + SHIPPING_COST_SEK
    assert sig["est_margin_sek"] == round(expected_revenue - expected_cost)


def test_arbitrage_signal_avoids_negative_margin_by_picking_best() -> None:
    """When all medians are close together, the signal still picks the best pair
    (even if net margin is negative after fees + shipping)."""
    platforms = [
        _platform("plick", count=10, median=1000),
        _platform("tradera", count=10, median=1050),
    ]
    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    # Best pair plick→tradera: revenue 945; cost 1079; margin −134 (negative,
    # but still the best of the two ordered pairs available)
    assert sig["est_margin_sek"] < 0


def test_arbitrage_signal_skips_platform_with_null_median() -> None:
    platforms = [
        _platform("plick", count=10, median=2000),
        {"platform": "vinted", "count": 10, "median_price": None},
        _platform("tradera", count=10, median=2500),
    ]
    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    assert sig["buy_platform"] == "plick"
    assert sig["sell_platform"] == "tradera"


# ── Sold-aware tests ─────────────────────────────────────────────────────────


def test_arbitrage_signal_uses_sold_median_when_available() -> None:
    """When the sell side has ≥5 sold listings, use sold median (more honest)."""
    platforms = [
        {"platform": "plick", "count": 10, "median_price": 2000,
         "sold_count": 0, "median_sold_price": None},
        {"platform": "tradera", "count": 10, "median_price": 3000,
         "sold_count": 12, "median_sold_price": 2600},  # sold for less than asked
    ]
    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    assert sig["sell_data_source"] == "sold"
    assert sig["sell_median_sek"] == 2600  # NOT 3000
    assert sig["buy_data_source"] == "listing"


def test_arbitrage_signal_falls_back_to_listing_median_when_no_sold_data() -> None:
    platforms = [
        _platform("plick", count=10, median=2000),
        _platform("vinted", count=10, median=2500),
    ]
    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    assert sig["sell_data_source"] == "listing"


def test_arbitrage_signal_ebay_buy_adds_shipping() -> None:
    from bourse.fees import EBAY_SHIPPING_TO_SE_SEK, PLATFORM_FEES, SHIPPING_COST_SEK
    platforms = [
        _platform("ebay", count=20, median=1500),
        {"platform": "tradera", "count": 10, "median_price": 2800,
         "sold_count": 8, "median_sold_price": 2500},
    ]
    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    assert sig["buy_platform"] == "ebay"
    assert sig["sell_platform"] == "tradera"
    # Cost = buy_median + base shipping + EBAY shipping
    expected_cost = 1500 + SHIPPING_COST_SEK + EBAY_SHIPPING_TO_SE_SEK
    # Revenue = sold_median × (1 − tradera fee 0.10)
    expected_revenue = 2500 * (1.0 - PLATFORM_FEES["tradera"])
    assert sig["est_margin_sek"] == round(expected_revenue - expected_cost)


def test_arbitrage_signal_ebay_sell_subtracts_shipping_from_revenue() -> None:
    from bourse.fees import EBAY_SHIPPING_TO_SE_SEK, PLATFORM_FEES, SHIPPING_COST_SEK
    platforms = [
        _platform("plick", count=10, median=1000),
        {"platform": "ebay", "count": 10, "median_price": 2500,
         "sold_count": 10, "median_sold_price": 2300},
    ]
    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    assert sig["buy_platform"] == "plick"
    assert sig["sell_platform"] == "ebay"
    expected_revenue = 2300 * (1.0 - PLATFORM_FEES["ebay"]) - EBAY_SHIPPING_TO_SE_SEK
    expected_cost = 1000 + SHIPPING_COST_SEK
    assert sig["est_margin_sek"] == round(expected_revenue - expected_cost)


def test_arbitrage_signal_buy_side_never_uses_sold_data() -> None:
    """You can't buy items that are already sold — buy side must use listing data only.

    Plick has heaps of sold data but a thin listing sample, so it's not
    eligible as the buy side; it can still be the sell side via its sold
    median.
    """
    platforms = [
        {"platform": "plick", "count": 2, "median_price": 1500,  # too low listing sample
         "sold_count": 20, "median_sold_price": 1200},
        _platform("tradera", count=10, median=2500),
    ]
    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    assert sig["buy_platform"] == "tradera"
    assert sig["sell_platform"] == "plick"
    assert sig["sell_data_source"] == "sold"
    assert sig["buy_data_source"] == "listing"
