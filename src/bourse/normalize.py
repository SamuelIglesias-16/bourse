"""Normalization helpers for scraped brand and size fields."""

from __future__ import annotations

import re

BRAND_VARIANTS = {
    "acne": "acne studios",
    "acne studio": "acne studios",
    "acne jeans": "acne studios",
    "acne studio stockholm": "acne studios",
    "acne studios stockholm": "acne studios",
    "maison": "maison margiela",
    "masion margiela": "maison margiela",
    "maison martin margiela": "maison margiela",
    "mm": "maison margiela",
    "margiela": "maison margiela",
    "maison marginal": "maison margiela",
    "mm6": "mm6",
    "mm6 maison margiela": "mm6",
    "stockholm": "acne studios",
}
UNKNOWN_BRANDS = {"—", "vet ej", "no brand", "fashion", "butik"}
SIZE_VARIANTS = {
    "xs/34/6": "XS",
    "34/6": "XS",
    "xxxs/30/2": "XS",
    "xxs/32/4": "XS",
    "s/36/8": "S",
    "36/8": "S",
    "m/38/10": "M",
    "38/10": "M",
    "l/40/12": "L",
    "40/12": "L",
    "xl/42/14": "XL",
    "42/14": "XL",
    "xxl/44/16": "XXL",
    "44/16": "XXL",
}
ONE_SIZE_VARIANTS = {"no size", "onesize", "one size"}


def _clean_text(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def normalize_brand(brand: str | None) -> str:
    """Map known brand aliases to canonical names."""
    key = _clean_text(brand)
    if not key or key in UNKNOWN_BRANDS:
        return ""
    return BRAND_VARIANTS.get(key, key)


def normalize_size(size: str | None) -> str:
    """Map common size aliases while preserving already-canonical values."""
    raw = "" if size is None else size.strip()
    if not raw:
        return ""

    key = raw.lower().replace(" ", "")
    if key in SIZE_VARIANTS:
        return SIZE_VARIANTS[key]
    if _clean_text(raw) in ONE_SIZE_VARIANTS:
        return "ONE SIZE"
    if re.fullmatch(r"W\d{2}", raw):
        return raw
    if raw.isdigit() and 35 <= int(raw) <= 47:
        return raw
    return raw
