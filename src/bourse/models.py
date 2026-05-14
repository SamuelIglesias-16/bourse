"""Shared listing model used by all platform scrapers."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, field_validator


class Listing(BaseModel):
    listing_id: str
    platform: str
    url: str
    title: str
    brand: Optional[str] = None
    size: Optional[str] = None
    condition: Optional[str] = None
    material: Optional[str] = None
    seller_name: str
    seller_rating: Optional[float] = None
    posted_at: Optional[datetime] = None   # Vinted exposes this; Plick does not
    price_sek: int
    likes: Optional[int] = None
    views: Optional[int] = None
    position_in_search: int
    scraped_at: datetime
    image_url: Optional[str] = None
    shipping_sek: Optional[int] = None  # eBay→SE shipping etc.; None for domestic-only platforms
    status_override: Optional[str] = None  # ingest path honours this — "sold" for ended Tradera/eBay sales
    raw_extras: Optional[dict[str, Any]] = None  # platform-specific fields; stored as JSON in listings.raw

    @field_validator("brand", mode="before")
    @classmethod
    def normalise_brand(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().lower() if v and v.strip() else None
