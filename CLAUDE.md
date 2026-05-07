# Bourse — Secondhand Fashion Market Intelligence

## What this project is

A personal tool (moving toward a product) that scrapes Swedish secondhand fashion marketplaces, builds a time-series database of listings and engagement signals, and surfaces analytics: sell-through rates, like-velocity, price trends, mispricing detection.

Built solo by Samuel (18 y/o, learning to build real systems). Claude Code and Codex are the primary dev tools. Always explain non-obvious decisions and suggest next steps after finishing a task.

---

## Current state (as of 2026-05-07)

Phase 2 is complete and partially into Phase 3 analytics:
- **Plick** and **Vinted** are both scraped and ingested
- **Postgres** (Supabase) is live and the primary data store for the API
- **FastAPI backend** is deployed on Railway
- **SQLite** is still used by the local CLI (`bourse` command)
- **Tradera scraper** is next (Codex is being assigned this)
- No automated cron job yet — scraping is triggered manually via API endpoints

---

## File map

| File | Purpose |
|------|---------|
| `src/bourse/api.py` | FastAPI app — all HTTP endpoints, startup logic |
| `src/bourse/alerts.py` | **[IN PROGRESS — Codex owned]** Price-drop and like-velocity alerts |
| `src/bourse/backfill.py` | `backfill_brands()` / `backfill_sizes()` — normalize existing Postgres data |
| `src/bourse/cli.py` | Typer CLI — all `bourse` terminal commands |
| `src/bourse/db.py` | SQLite schema DDL and connection helper |
| `src/bourse/ingest.py` | Filter, normalize, and write listings to SQLite; defines `PRICE_MIN`/`PRICE_MAX` |
| `src/bourse/migrate.py` | One-time script to copy SQLite → Postgres |
| `src/bourse/models.py` | Shared `Listing` pydantic model used by all scrapers |
| `src/bourse/normalize.py` | `normalize_brand()` and `normalize_size()` — canonical mappings |
| `src/bourse/pg_report.py` | Postgres-backed report queries consumed by `GET /report` |
| `src/bourse/plick.py` | Plick.se scraper (HTML via httpx + selectolax) |
| `src/bourse/report.py` | SQLite-backed analytics + Rich terminal tables for the CLI |
| `src/bourse/scrape_tasks.py` | Background scrape tasks for the API; pg read/write helpers; watchlist run tracking |
| `src/bourse/tradera.py` | Tradera.se scraper (embedded JSON in search-page HTML, via httpx + selectolax) |
| `src/bourse/vinted.py` | Vinted.se scraper (JSON API via curl-cffi with Chrome TLS fingerprint) |
| `tests/test_alerts.py` | **[IN PROGRESS — Codex owned]** Tests for alerts.py |
| `tests/test_normalize.py` | 16 brand + 18 size normalization tests |

---

## Tech stack

- **Python 3.11+** for everything
- **httpx** — Plick HTTP requests
- **curl-cffi** — Vinted requests (Chrome TLS fingerprint to pass Cloudflare)
- **selectolax** — HTML parsing for Plick
- **pydantic v2** — `Listing` model validation
- **typer** — CLI
- **polars** — analytics in the CLI report
- **rich** — terminal table rendering
- **FastAPI + uvicorn** — HTTP API
- **psycopg2-binary** — Postgres driver
- **python-dotenv** — loads `.env` locally; no-op on Railway
- **SQLite** — local dev / CLI storage (`bourse.db` at project root)
- **Postgres** (Supabase) — production data store

---

## Environment variables

Create `.env` at the project root (never commit it):

```
DATABASE_URL=postgres://...  # Supabase Session Pooler connection string
```

`DATABASE_URL` is required for the API to start. The CLI uses SQLite and doesn't need it.

On Railway, `DATABASE_URL` is set as an environment variable — `load_dotenv()` is a no-op there.

---

## Database schema

### SQLite (local CLI — `bourse.db`)

```sql
CREATE TABLE listings (
    listing_id    TEXT PRIMARY KEY,       -- "{platform}:{platform_id}"
    platform      TEXT NOT NULL,          -- plick | vinted | blocket | tradera
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    brand         TEXT,                   -- normalized via normalize_brand()
    size          TEXT,                   -- normalized via normalize_size()
    condition     TEXT,
    material      TEXT,
    seller_name   TEXT NOT NULL,
    seller_rating REAL,
    posted_at     TIMESTAMP,              -- Vinted exposes this; Plick/Plick don't
    first_seen    TIMESTAMP NOT NULL,
    last_seen     TIMESTAMP NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',  -- active | sold | removed | unknown
    raw           TEXT                    -- JSON blob for extra platform fields
);

CREATE TABLE listing_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id         TEXT NOT NULL REFERENCES listings(listing_id),
    scraped_at         TIMESTAMP NOT NULL,
    price_sek          INTEGER NOT NULL,
    likes              INTEGER,
    views              INTEGER,
    position_in_search INTEGER
);
-- Never UPDATE or DELETE snapshots. Only INSERT.
```

### Postgres (Supabase — API production)

Same schema as SQLite with these differences:
- `listing_snapshots.id` is `SERIAL PRIMARY KEY` (not `AUTOINCREMENT`)
- All timestamps stored as native `TIMESTAMP` (not ISO strings)

Additional tables created automatically at API startup:
```sql
CREATE TABLE IF NOT EXISTS watchlist_runs (
    query          TEXT PRIMARY KEY,
    last_run_at    TIMESTAMP NOT NULL,
    listings_found INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watchlist (
    id    SERIAL PRIMARY KEY,
    query TEXT UNIQUE NOT NULL
);
-- Seeded from watchlist.txt on first boot (when table is empty).
-- This is the source of truth on Railway; watchlist.txt is kept as a fallback.
```

---

## Price sanity filter

**`PRICE_MIN = 200`, `PRICE_MAX = 50_000`** — defined in `ingest.py`, imported everywhere.

Listings outside this range (bud listings, joke prices, "trade only") are:
- Excluded by `ingest()` before writing to SQLite
- Excluded from all Postgres aggregation queries in `pg_report.py`
- Excluded from `/listings/by-query/stats` calculations
- Excluded from `GET /listings` when `?sane_prices=true` is passed

---

## Normalization

`normalize.py` runs at the start of every `ingest()` call, before filtering and DB writes:

- **`normalize_brand(brand)`** — maps aliases to canonical names (`"acne"` → `"acne studios"`, `"MM"` → `"maison margiela"`, etc.). Returns `""` for blanks, `"—"`, or known junk brands (`"fashion"`, `"butik"`).
- **`normalize_size(size)`** — maps compound sizes (`"xs/34/6"` → `"XS"`, `"38/10"` → `"M"`). Preserves jeans waist sizes (`W30`), shoe sizes (`35`–`47`), and passthrough values. Returns `""` for blank/None.

Both functions accept `str | None` and always return `str`.

---

## Watchlist

**Source of truth on Railway: Postgres `watchlist` table** (created at API startup).  
**Source of truth locally: `watchlist.txt`** (one query per line, `#` for comments).

On first Railway deploy, the `watchlist` table is seeded from `watchlist.txt` if it's empty. After that, `watchlist.txt` is only read as a fallback when Postgres is unreachable.

The scraping tasks (`run_scrape_new`, `run_scrape_update`) call `pg_read_watchlist()`, which reads from Postgres and falls back to `watchlist.txt`.

**CLI** still writes to `watchlist.txt` (local dev only):
```bash
bourse watchlist add "maison margiela gats"
bourse watchlist remove "acne studios hoodie"
```

**API** reads/writes Postgres `watchlist` table:
```
POST   /watchlist    {query: str}  → adds to Postgres
DELETE /watchlist/{query}          → removes from Postgres
```

---

## CLI commands

All commands run via `bourse` (installed as a script from `pyproject.toml`).

### Scraping (targets SQLite locally)

```bash
bourse scrape plick "acne studios"          # scrape Plick for one query
bourse scrape plick --watchlist             # scrape Plick for all watchlist queries
bourse scrape plick --watchlist --pages 10  # more pages per query

bourse scrape vinted "acne studios"         # same for Vinted
bourse scrape vinted --watchlist

bourse scrape all --watchlist               # both platforms, all queries
bourse scrape all --watchlist --pages 10
```

### Watchlist management

```bash
bourse watchlist add "maison margiela gats"
bourse watchlist remove "acne studios hoodie"
```

### Report (SQLite-backed terminal tables)

```bash
bourse report          # top 10 per section
bourse report --full   # all rows
```

### Database utilities

```bash
bourse db clean             # remove listings with no brand that match no watchlist keywords
bourse db clean-prices      # remove listings with price outside 200–50,000 SEK (SQLite)
bourse db backfill-brands   # normalize brand field for all listings in Postgres
bourse db backfill-sizes    # normalize size field for all listings in Postgres
```

Both `db clean` and `db clean-prices` show a preview and require confirmation before deleting.

---

## API endpoints

Base URL locally: `http://localhost:8000`  
Base URL on Railway: set when deployed (check Railway dashboard).

### Watchlist

```
GET  /watchlist
→ [{query: str, last_scraped_at: str|null, listings_found: int|null}]

POST /watchlist
Body: {query: str}
→ {query: str, total: int}
Status: 201

DELETE /watchlist/{query}
→ {removed: str, remaining: int}
```

### Scrape triggers (all return 202 Accepted, run in background)

```
POST /scrape         → full scrape (both platforms, all queries, via subprocess)
POST /scrape/new     → new listings only (stops per query when page is all known IDs)
POST /scrape/update  → re-fetches all active listings to update price/likes/status
```

After `POST /scrape/new`, `watchlist_runs` is upserted per query with exact new-listing count.  
After `POST /scrape/update`, `watchlist_runs` is upserted for all queries with updated timestamp only.

### Listings

```
GET /listings
Query params (all optional):
  platform=plick|vinted
  brand=acne+studios
  category=hoodie|longsleeve|t-shirt|jeans|jacket|other
  min_price=500
  max_price=2000
  sane_prices=true          ← excludes price < 200 or > 50000
→ [{listing_id, platform, url, title, brand, size, condition,
    price_sek, likes, status, days_on_market, first_seen, last_seen, category}]

GET /listings/by-query?q=acne+studios+hoodie
→ same shape as GET /listings, keyword OR-match on title+brand

GET /listings/by-query/stats?q=acne+studios+hoodie
→ {total_listings, avg_price, min_price, max_price, avg_likes,
   top_liked_title, top_liked_price, top_liked_likes, top_liked_url}
   (always filters by sane prices before computing stats)

GET /listings/{listing_id}/history
→ [{id, listing_id, scraped_at, price_sek, likes, views, position_in_search}]
Status 404 if not found.
```

### Report

```
GET /report
→ {
    sell_through_by_brand: [{brand, sell_through_pct, total_listings, sold_listings}],
    best_day_to_list:      [{day, sell_through_pct, avg_dom}],  -- Mon through Sun
    avg_price_by_category: [{category, avg_price_sek, listing_count}],
    like_velocity:         [{title, platform, likes_gained, days_tracked, velocity_per_day}],
    top_liked:             [{title, platform, price_sek, likes, days_on_market, url}]
  }
All report data uses only active listings with sane prices (200–50,000 SEK).
```

---

## Scraping pipeline (end to end)

### New listings flow (`POST /scrape/new` or `bourse scrape all`)

1. Read watchlist via `pg_read_watchlist()` (Postgres → falls back to `watchlist.txt`)
2. Fetch all known `listing_id`s from Postgres (to skip already-seen listings)
3. For each query:
   - **Plick**: fetch HTML search pages via httpx, parse with selectolax, extract cards → fetch detail page for each new card (condition, material, likes, seller_rating, price from JSON-LD)
   - **Vinted**: fetch JSON from `/api/v2/catalog/items` via curl-cffi (Chrome TLS impersonation), parse items directly
   - Stop scraping pages as soon as an entire page contains only known IDs
4. Filter results: keyword match + price sanity (200–50,000 SEK) + normalize brand/size
5. Upsert to `listings` + insert to `listing_snapshots` in Postgres
6. Upsert `watchlist_runs` for the query with timestamp + count

### Update flow (`POST /scrape/update`)

1. Fetch all `(listing_id, url)` pairs with `status = 'active'` from Postgres
2. For each listing:
   - **Plick**: `GET {url}` → parse detail page → extract current price + likes
   - **Vinted**: `GET /api/v2/items/{item_id}` → extract price + likes
   - **Tradera**: `GET {url}` → parse `__NEXT_DATA__` JSON → extract price + bid count; `(None, None)` return → mark removed
   - If 404/410 or `None` price: mark listing `status = 'removed'`
   - Else: insert new snapshot row with current price/likes
3. Upsert `watchlist_runs` for all watchlist queries (timestamp only, preserve `listings_found`)

### Caching

- Plick: HTML cached to `.cache/{sha256[:20]}.html` for 24h
- Vinted: JSON cached to `.cache/{sha256[:20]}.json` for 24h
- `fetch_listing()` in both scrapers bypasses cache (always fetches live for updates)

### Rate limiting

| Platform | Delay | Backoff |
|----------|-------|---------|
| Plick | 2–4s random between requests | 2s → 4s → 8s → abort |
| Vinted | 1–2s random between requests | 2s → 4s → 8s → abort |

Never parallel. Always single-threaded.

---

## Deployment

### Railway (production)

- **Service**: FastAPI app via uvicorn
- **Start command**: `uvicorn bourse.api:app --host 0.0.0.0 --port $PORT`
- **Builder**: NIXPACKS (auto-detects Python, installs from `requirements.txt`)
- **Health check**: `GET /watchlist` with 30s timeout
- **Restart policy**: ON_FAILURE
- **Config**: `railway.toml` + `Procfile` at project root

### What's on Railway

- The FastAPI API (`api.py`)
- All scraping endpoints (`/scrape`, `/scrape/new`, `/scrape/update`)
- Postgres connection via `DATABASE_URL` env var

### What's still local only

- The `bourse` CLI (SQLite-backed)
- `bourse.db` (SQLite database)
- `migrate.py` (run once to move data from SQLite → Postgres)
- `.cache/` directory (scraper HTTP cache)

### Running the migration (SQLite → Postgres)

```bash
# Run once to copy existing SQLite data to Postgres
python -m bourse.migrate
```

---

## Running locally

```bash
# Install in dev mode
pip install -e ".[dev]"

# Start the API (requires .env with DATABASE_URL)
uvicorn bourse.api:app --reload

# Run CLI commands
bourse scrape all --watchlist
bourse report

# Run tests
pytest tests/
```

---

## Code conventions

- **Type hints everywhere** — no untyped functions
- **Files ≤ 300 lines, functions ≤ 40 lines** — split when approaching limit
- **`logging` not `print`** — except in CLI output (typer.echo)
- **No comments unless the WHY is non-obvious** — names should be self-explanatory
- **Never UPDATE or DELETE snapshots** — only INSERT
- **Postgres for API/prod** — `pg_read()` / `pg_write()` context managers in `scrape_tasks.py`
- **SQLite for CLI** — `get_connection(DB_PATH)` in `db.py`
- **psycopg2.OperationalError falls back to SQLite** — `_write_to_pg()` and snapshot helpers have this fallback
- **`PRICE_MIN` / `PRICE_MAX` imported from `ingest.py`** — never hardcode 200 or 50000 elsewhere

---

## Known issues / in progress

- No automated cron job on Railway yet — scraping must be triggered manually via API
- `bourse db clean` and `bourse db clean-prices` only work on SQLite (local); no Postgres equivalent yet
- `migrate.py` is a one-time script with no idempotency for updates — if run twice it silently skips duplicates but doesn't update changed fields
- Plick search results ordering is "relevance" which changes — position_in_search is approximate
- Vinted `posted_at` comes from photo timestamp (a proxy, not the true listing date)

---

## Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| 1 | ✅ Done | Plick scraper, SQLite, CLI |
| 2 | ✅ Done | Vinted scraper, Postgres, FastAPI, Railway deploy, normalization |
| 2.5 | ✅ Done | Tradera scraper, wired into API and CLI |
| 3 | 🔄 In progress | Alerts (`alerts.py` — Codex owned); hedonic pricing model next |
| 4 | Planned | Web UI (Lovable), scheduled scraping (cron on Railway), multi-user |

### Immediate next tasks

1. **Railway cron** — set up Railway's cron to call `POST /scrape/new` daily and `POST /scrape/update` every few hours
2. **Postgres `clean-prices` command** — equivalent of `bourse db clean-prices` but targets the live Postgres DB
3. **`price_alerts` to Postgres** — currently `alerts.py` uses SQLite (`bourse.db`). On Railway, `bourse.db` is not persisted across deploys. Move price alerts to the Postgres DB so they survive redeployment.

---

## Note on AGENTS.md

`AGENTS.md` exists at the project root with the same content as `CLAUDE.md`. Keep both files in sync — Codex reads `AGENTS.md` and Claude Code reads `CLAUDE.md`.
