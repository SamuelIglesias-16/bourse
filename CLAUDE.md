# Bourse — Secondhand Fashion Market Intelligence

## What this project is
A personal tool (eventually a product) that scrapes Swedish secondhand fashion
marketplaces, builds a time-series database of listings + engagement signals,
and surfaces analytics: hedonic pricing, mispricing detection, demand trends.

Built solo by Samuel. Currently personal-use phase — no users besides me.

## Tech stack
- Python 3.11+ for everything backend
- SQLite for storage in Phase 1, migrate to Postgres in Phase 2
- `httpx` for HTTP requests
- `selectolax` for HTML parsing
- `pydantic` for data validation
- `typer` for the CLI
- `polars` for analytics

## Roadmap (current phase: 1)
- **Phase 1** — Plick scraper, SQLite, CLI. *Where we are now.*
- **Phase 2** — Add Vinted + Blocket. Normalized schema. Postgres. Image hashing.
- **Phase 3** — Hedonic pricing model, like-velocity scoring, mispricing detection.
- **Phase 4** — Web UI (Lovable), cloud workers (Codex), multi-user.

Don't build Phase 2+ features yet. Ask before any scope creep.

## Database schema

`listings` — one row per unique listing
- `listing_id` TEXT PRIMARY KEY (format: `{platform}:{platform_id}`)
- `platform` TEXT (plick, vinted, blocket)
- `url` TEXT
- `title` TEXT
- `brand` TEXT (nullable, normalized lowercase)
- `size` TEXT (nullable)
- `condition` TEXT (nullable)
- `material` TEXT (nullable)
- `seller_name` TEXT
- `seller_rating` REAL (nullable)
- `first_seen` TIMESTAMP
- `last_seen` TIMESTAMP
- `status` TEXT (active, sold, removed, unknown)
- `raw` JSON (extra platform fields)

`listing_snapshots` — one row per scrape per listing
- `id` INTEGER PRIMARY KEY AUTOINCREMENT
- `listing_id` TEXT (FK to listings)
- `scraped_at` TIMESTAMP
- `price_sek` INTEGER
- `likes` INTEGER (nullable)
- `views` INTEGER (nullable)
- `position_in_search` INTEGER (nullable)

Never UPDATE or DELETE snapshots. Only INSERT.

## Scraping rules
- 2–4 second random delay between requests
- Realistic User-Agent, rotate among 3–4 common ones
- No parallel requests — single-threaded only
- If a request fails, back off exponentially. Three failures → abort.
- Never log in. Public listing data only.
- Cache HTML responses for 24h during development.

## Code style
- Type hints everywhere
- Functions under 40 lines, files under 300 lines
- Tests with pytest for parsers
- Use `logging` not `print` except in CLI output

## How I want to work with you
- Before writing significant code, briefly tell me the plan
- After changes, summarize what changed and tell me what to run
- I'm 18, new to building real systems — explain non-obvious decisions
- Suggest the next logical step after finishing each task
