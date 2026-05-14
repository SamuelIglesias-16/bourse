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
    """Pick the (buy, sell) pair maximizing net median spread, sold-aware.

    Each platform dict may include:
      - ``count`` / ``median_price`` (active-listing aggregates)
      - ``sold_count`` / ``median_sold_price`` (status='sold' aggregates over last 90d)

    Sell-side revenue uses the SOLD median when ``sold_count >= 5`` (more
    honest — what items actually go for), falling back to the listing median.
    Buy-side cost always uses the listing median (you can only buy what's
    actively listed). Per-side ``data_source`` reflects which median was used.

    When buy_platform == 'ebay' we add ``EBAY_SHIPPING_TO_SE_SEK`` to cost
    (international shipping to Sweden). When sell_platform == 'ebay' the
    same amount is subtracted from revenue (the seller covers it).

    Returns ``None`` when fewer than two platforms have sample ≥5 on either
    sold or listing data.
    """
    from bourse.fees import EBAY_SHIPPING_TO_SE_SEK  # local import

    eligible = [
        p for p in platforms
        if (p.get("count", 0) >= 5 and p.get("median_price") is not None)
        or (p.get("sold_count", 0) >= 5 and p.get("median_sold_price") is not None)
    ]
    if len(eligible) < 2:
        return None

    best: dict[str, Any] | None = None
    for buy in eligible:
        # Buy side always uses listing median — you can't purchase a sold listing.
        if buy.get("median_price") is None or buy.get("count", 0) < 5:
            continue
        buy_median = buy["median_price"]
        for sell in eligible:
            if sell["platform"] == buy["platform"]:
                continue

            sell_uses_sold = (
                sell.get("sold_count", 0) >= 5
                and sell.get("median_sold_price") is not None
            )
            if sell_uses_sold:
                sell_median = sell["median_sold_price"]
                sell_source = "sold"
            elif sell.get("median_price") is not None:
                sell_median = sell["median_price"]
                sell_source = "listing"
            else:
                continue

            sell_fee = PLATFORM_FEES.get(sell["platform"], 0.0)
            revenue = sell_median * (1.0 - sell_fee)
            cost = buy_median + SHIPPING_COST_SEK

            # Cross-border shipping when crossing eBay
            if buy["platform"] == "ebay":
                cost += EBAY_SHIPPING_TO_SE_SEK
            if sell["platform"] == "ebay":
                revenue -= EBAY_SHIPPING_TO_SE_SEK

            est_margin = revenue - cost
            gross_margin = sell_median - buy_median
            candidate = {
                "buy_platform": buy["platform"],
                "sell_platform": sell["platform"],
                "buy_median_sek": buy_median,
                "sell_median_sek": round(sell_median),
                "buy_data_source": "listing",
                "sell_data_source": sell_source,
                "est_margin_sek": round(est_margin),
                "est_margin_pct": round((est_margin / cost) * 100, 1) if cost > 0 else None,
                "gross_margin_sek": round(gross_margin),
            }
            if best is None or candidate["est_margin_sek"] > best["est_margin_sek"]:
                best = candidate
    return best
