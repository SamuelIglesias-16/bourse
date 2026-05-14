"""Platform-specific fee + shipping defaults used to compute net arbitrage margins.

PLATFORM_FEES is the fraction of the sale price each platform takes from the seller.
SHIPPING_COST_SEK is a flat per-transaction estimate the buyer (us, when buying to
resell) covers between platforms. Both are tunable here; the /platforms/compare
endpoint reads them at request time so changes take effect without restart.

Real-world notes (verify periodically):
  - plick: no seller fee for private listings (0%)
  - vinted: ~5% protection fee on each sale
  - tradera: ~10% platform fee on the sale price
  - blocket: no seller fee for most private categories (0%)
"""

from __future__ import annotations

PLATFORM_FEES: dict[str, float] = {
    "plick": 0.0,
    "vinted": 0.05,
    "tradera": 0.10,
    "blocket": 0.0,
    "ebay": 0.13,  # eBay final value fee + payment processing, rough average
}

SHIPPING_COST_SEK: int = 79

# Currency: eBay.com items are priced in USD; converted once at ingest using
# this rate. Bump it manually if the SEK/USD rate drifts materially.
USD_TO_SEK: float = 10.5

# International shipping eBay → Sweden, flat-rate default. Used by the
# arbitrage signal when buy_platform or sell_platform is eBay.
EBAY_SHIPPING_TO_SE_SEK: int = 250


def fee_for(platform: str) -> float:
    """Return the seller fee fraction for *platform*, defaulting to 0.0 if unknown."""
    return PLATFORM_FEES.get(platform, 0.0)
