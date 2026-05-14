"""Pure-Python helpers for the /platforms/compare endpoint.

Kept separate from api.py so they can be unit-tested without booting FastAPI
or requiring DATABASE_URL.
"""

from __future__ import annotations

from typing import Any

from bourse.fees import PLATFORM_FEES, SHIPPING_COST_SEK


def confidence_tier(count: int) -> str:
    """Map a per-platform sample size to a confidence label."""
    if count < 5:
        return "low"
    if count <= 15:
        return "medium"
    return "high"


def compute_arbitrage_signal(platforms: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the (buy, sell) pair across platforms maximizing net median spread.

    Both sides use the median price — this models a *sustainable* arbitrage
    opportunity ("if you buy at the typical price here and sell at the typical
    price there"), not a lucky single-listing deal. Both sides of the pair
    must have ``count >= 5`` (medium+ confidence). Net margin deducts the
    sell platform's fee plus a flat shipping cost. Returns ``None`` when
    fewer than two platforms qualify.
    """
    eligible = [
        p for p in platforms
        if p.get("count", 0) >= 5 and p.get("median_price") is not None
    ]
    if len(eligible) < 2:
        return None

    best: dict[str, Any] | None = None
    for buy in eligible:
        buy_median = buy["median_price"]
        for sell in eligible:
            if sell["platform"] == buy["platform"]:
                continue
            sell_fee = PLATFORM_FEES.get(sell["platform"], 0.0)
            sell_median = sell["median_price"]
            revenue = sell_median * (1.0 - sell_fee)
            cost = buy_median + SHIPPING_COST_SEK
            est_margin = revenue - cost
            gross_margin = sell_median - buy_median
            candidate = {
                "buy_platform": buy["platform"],
                "sell_platform": sell["platform"],
                "buy_median_sek": buy_median,
                "sell_median_sek": sell_median,
                "est_margin_sek": round(est_margin),
                "est_margin_pct": round((est_margin / cost) * 100, 1) if cost > 0 else None,
                "gross_margin_sek": round(gross_margin),
            }
            if best is None or candidate["est_margin_sek"] > best["est_margin_sek"]:
                best = candidate
    return best
