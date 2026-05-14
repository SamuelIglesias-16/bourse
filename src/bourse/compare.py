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
    """Pick the (buy, sell) pair across platforms maximizing net margin.

    Each platform dict must include: ``platform``, ``count``, ``median_price``,
    and ``cheapest_3`` (a list whose first element exposes ``price_sek``).
    Both sides of the pair must have ``count >= 5`` (medium+ confidence).
    Net margin deducts the sell platform's fee plus a flat shipping cost.
    Returns ``None`` when fewer than two platforms qualify.
    """
    eligible = [
        p for p in platforms
        if p.get("count", 0) >= 5
        and p.get("cheapest_3")
        and p.get("median_price") is not None
    ]
    if len(eligible) < 2:
        return None

    best: dict[str, Any] | None = None
    for buy in eligible:
        cheapest_buy = buy["cheapest_3"][0]["price_sek"]
        for sell in eligible:
            if sell["platform"] == buy["platform"]:
                continue
            sell_fee = PLATFORM_FEES.get(sell["platform"], 0.0)
            sell_median = sell["median_price"]
            revenue = sell_median * (1.0 - sell_fee)
            cost = cheapest_buy + SHIPPING_COST_SEK
            est_margin = revenue - cost
            gross_margin = sell_median - cheapest_buy
            candidate = {
                "buy_platform": buy["platform"],
                "sell_platform": sell["platform"],
                "buy_price_sek": cheapest_buy,
                "sell_median_sek": sell_median,
                "est_margin_sek": round(est_margin),
                "est_margin_pct": round((est_margin / cost) * 100, 1) if cost > 0 else None,
                "gross_margin_sek": round(gross_margin),
            }
            if best is None or candidate["est_margin_sek"] > best["est_margin_sek"]:
                best = candidate
    return best
