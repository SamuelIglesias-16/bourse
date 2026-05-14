"""Seed the Postgres watchlist with the curated arbitrage query set.

Idempotent: queries already in the watchlist are skipped (server returns 409,
we ignore). Reads BOURSE_API_URL from the environment (default localhost:8000).

Run:
    python3 -m bourse.seed_watchlist
    BOURSE_API_URL=https://bourse-production.up.railway.app python3 -m bourse.seed_watchlist
"""

from __future__ import annotations

import logging
import os
import sys

import httpx

logger = logging.getLogger(__name__)

# Curated for inter-platform arbitrage: tech-heavy queries lean on Blocket's
# strength while designer/sneakers/cameras lean on Plick/Vinted/Tradera.
QUERIES: list[str] = [
    # tech (10)
    "airpods pro 2",
    "airpods max",
    "iphone 14 pro",
    "macbook air m2",
    "ipad pro",
    "apple watch ultra",
    "playstation 5",
    "nintendo switch oled",
    "sony wh-1000xm5",
    "fujifilm x100v",
    # designer (6)
    "maison margiela tabi",
    "acne studios jeans",
    "our legacy",
    "stussy hoodie",
    "carhartt wip detroit",
    "arcteryx beta",
    # sneakers (4)
    "jordan 1",
    "adidas samba",
    "new balance 990",
    "salomon xt-6",
    # watches (2)
    "g-shock",
    "seiko diver",
    # cameras (4)
    "contax t2",
    "olympus mju",
    "ricoh gr",
    "canon ae-1",
    # audio (2)
    "marantz",
    "sennheiser hd600",
    # outdoor (2)
    "patagonia retro x",
    "carhartt double knee",
]


def seed(api_url: str) -> dict[str, int]:
    """POST each curated query to /watchlist. Returns counts of added vs skipped."""
    added = 0
    skipped = 0
    failed = 0
    with httpx.Client(base_url=api_url, timeout=30) as client:
        for query in QUERIES:
            try:
                resp = client.post("/watchlist", json={"query": query})
            except httpx.HTTPError as exc:
                logger.error("Network error adding %r: %s", query, exc)
                failed += 1
                continue
            if resp.status_code == 201:
                added += 1
                logger.info("Added %r", query)
            elif resp.status_code == 409:
                skipped += 1
                logger.info("Already present: %r", query)
            else:
                failed += 1
                logger.error("Unexpected %d adding %r: %s", resp.status_code, query, resp.text)
    return {"added": added, "skipped": skipped, "failed": failed, "total": len(QUERIES)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s — %(message)s")
    api_url = os.environ.get("BOURSE_API_URL", "http://localhost:8000")
    logger.info("Seeding %d queries to %s", len(QUERIES), api_url)
    result = seed(api_url)
    logger.info(
        "Done. added=%d skipped=%d failed=%d (total=%d)",
        result["added"], result["skipped"], result["failed"], result["total"],
    )
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
