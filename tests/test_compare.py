"""Unit tests for the /platforms/compare business logic."""

from __future__ import annotations

import pytest

from bourse.compare import compute_arbitrage_signal, confidence_tier
from bourse.fees import PLATFORM_FEES, SHIPPING_COST_SEK


def _platform(
    name: str,
    *,
    count: int,
    median: int,
    cheapest: int,
) -> dict:
    return {
        "platform": name,
        "count": count,
        "median_price": median,
        "cheapest_3": [{"price_sek": cheapest}],
    }


def test_confidence_tier_thresholds() -> None:
    assert confidence_tier(0) == "low"
    assert confidence_tier(4) == "low"
    assert confidence_tier(5) == "medium"
    assert confidence_tier(15) == "medium"
    assert confidence_tier(16) == "high"
    assert confidence_tier(1000) == "high"


def test_arbitrage_signal_picks_highest_margin_pair() -> None:
    platforms = [
        _platform("plick", count=10, median=2000, cheapest=1500),
        _platform("vinted", count=10, median=2500, cheapest=2300),
        _platform("tradera", count=10, median=2800, cheapest=2700),
    ]

    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    assert sig["buy_platform"] == "plick"
    # Sell on tradera @ median 2800 with 10% fee → revenue 2520
    # Cost 1500 + 79 shipping = 1579 → margin ~941
    # Sell on vinted @ median 2500 with 5% fee → revenue 2375; cost 1579 → margin 796
    assert sig["sell_platform"] == "tradera"
    assert sig["est_margin_sek"] > 0


def test_arbitrage_signal_returns_none_when_only_one_platform_eligible() -> None:
    platforms = [
        _platform("plick", count=10, median=2000, cheapest=1500),
        _platform("vinted", count=2, median=2500, cheapest=2300),  # low sample
    ]
    assert compute_arbitrage_signal(platforms) is None


def test_arbitrage_signal_returns_none_when_all_low_sample() -> None:
    platforms = [
        _platform("plick", count=3, median=2000, cheapest=1500),
        _platform("vinted", count=4, median=2500, cheapest=2300),
    ]
    assert compute_arbitrage_signal(platforms) is None


def test_arbitrage_signal_returns_none_for_empty_input() -> None:
    assert compute_arbitrage_signal([]) is None


def test_arbitrage_signal_net_includes_fees_and_shipping() -> None:
    """Vinted sell side (5%): revenue should be median × 0.95, then subtract shipping."""
    platforms = [
        _platform("plick", count=10, median=1000, cheapest=900),
        _platform("vinted", count=10, median=1500, cheapest=1400),
    ]
    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    # The best pair will be plick→vinted: revenue 1500×0.95=1425; cost 900+79=979; margin 446
    assert sig["buy_platform"] == "plick"
    assert sig["sell_platform"] == "vinted"
    expected_revenue = 1500 * (1.0 - PLATFORM_FEES["vinted"])
    expected_cost = 900 + SHIPPING_COST_SEK
    expected_margin = round(expected_revenue - expected_cost)
    assert sig["est_margin_sek"] == expected_margin
    # gross omits fee + shipping
    assert sig["gross_margin_sek"] == 1500 - 900


def test_arbitrage_signal_works_with_all_four_platforms() -> None:
    """Spec calls for evaluating 12 ordered pairs across 4 platforms."""
    platforms = [
        _platform("blocket", count=20, median=1800, cheapest=1200),  # cheapest buy
        _platform("plick", count=10, median=2000, cheapest=1700),
        _platform("vinted", count=10, median=2400, cheapest=2200),
        _platform("tradera", count=10, median=2800, cheapest=2600),  # highest sell
    ]
    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    # Cheapest is Blocket@1200; highest median net of fees is Tradera@2800×0.9=2520
    # Revenue 2520, cost 1200+79=1279, margin 1241
    assert sig["buy_platform"] == "blocket"
    assert sig["sell_platform"] == "tradera"
    assert sig["est_margin_sek"] > 1000


def test_arbitrage_signal_skips_platform_without_cheapest_3() -> None:
    platforms = [
        _platform("plick", count=10, median=2000, cheapest=1500),
        {
            "platform": "vinted",
            "count": 10,
            "median_price": 2400,
            "cheapest_3": [],  # missing
        },
        _platform("tradera", count=10, median=2500, cheapest=2300),
    ]
    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    assert sig["buy_platform"] == "plick"
    assert sig["sell_platform"] == "tradera"


def test_arbitrage_signal_skips_platform_with_null_median() -> None:
    platforms = [
        _platform("plick", count=10, median=2000, cheapest=1500),
        {
            "platform": "vinted",
            "count": 10,
            "median_price": None,
            "cheapest_3": [{"price_sek": 2300}],
        },
        _platform("tradera", count=10, median=2500, cheapest=2300),
    ]
    sig = compute_arbitrage_signal(platforms)
    assert sig is not None
    assert sig["sell_platform"] == "tradera"
