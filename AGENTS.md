# Bourse — Secondhand Fashion Market Intelligence

## What this project is

A personal tool (moving toward a product) that scrapes Swedish secondhand fashion marketplaces, builds a time-series database of listings and engagement signals, and surfaces analytics: sell-through rates, like-velocity, price trends, mispricing detection.

Built solo by Samuel (18 y/o, learning to build real systems). Claude Code and Codex are the primary dev tools. Always explain non-obvious decisions and suggest next steps after finishing a task.

---

## Current state (as of 2026-05-12)

Phase 2.5 complete, Phase 3 alerts shipped, Phase 4 web UI underway:
- **Plick**, **Vinted**, **Tradera**, and **Blocket** all scraped, ingested, and fully wired into both new-listing and update flows
- **Postgres** (Supabase) is live and the primary data store for the API
- **FastAPI backend** deployed on Railway at `https://bourse-production.up.railway.app`
- **SQLite** is still used by the local CLI (`bourse` command)
- **Telegram alerts** working — `check_alerts()` fires after every `POST /scrape/new` run
- **Watchlist** is now Postgres-backed on Railway; `watchlist.txt` kept as a fallback
- **Cron entry point** at `src/bourse/cron.py` (`python -m bourse.cron` with `CRON_JOB=scrape-new|scrape-update`); Railway cron services still need to be configured in the dashboard
- **Cross-platform comparison** endpoint `/platforms/compare?q=…` — returns p25/median/p75/spread per platform, top-3 cheapest listings per platform, sample confidence tiers, and a top-level `arbitrage_signal` recommending the best buy→sell platform pair net of fees + shipping
- **Price history** endpoint `/platforms/compare/history?q=…&days=30` — feeds spark lines on the frontend
- **React frontend** lives in `~/projects/garms-glance` (TanStack Start + Tailwind v4 + shadcn). Heavily redesigned: light editorial theme (cream + terracotta), custom hoodie spinner on `/landing` (canvas-based 24fps frame sequence), and a 4-platform `/compare` page consuming the new endpoint. See `SESSION_NOTES.md` for handoff details.

---

## File map

| File | Purpose |
|------|---------|
| `src/bourse/api.py` | FastAPI app — all HTTP endpoints, startup logic |
| `src/bourse/alert_routes.py` | `APIRouter` for `GET/POST/DELETE /alerts` endpoints |
| `src/bourse/alerts.py` | Price alert matching and Telegram delivery; rate-limit handling |
| `src/bourse/backfill.py` | `backfill_brands()` / `backfill_sizes()` — normalize existing Postgres data |
| `src/bourse/blocket.py` | Blocket.se scraper (embedded `__NEXT_DATA__` JSON via httpx; curl-cffi Cloudflare fallback) |
| `src/bourse/cli.py` | Typer CLI — all `bourse` terminal commands |
| `src/bourse/compare.py` | Pure-Python helpers for `/platforms/compare` (confidence tiers, arbitrage signal) |
| `src/bourse/cron.py` | Cron entry point — runs `run_scrape_new` or `run_scrape_update` based on `CRON_JOB` env var |
| `src/bourse/db.py` | SQLite schema DDL and connection helper |
| `src/bourse/fees.py` | `PLATFORM_FEES` + `SHIPPING_COST_SEK` constants used by the arbitrage signal |
| `src/bourse/ingest.py` | Filter, normalize, and write listings to SQLite; defines `PRICE_MIN`/`PRICE_MAX` |
| `src/bourse/migrate.py` | One-time script to copy SQLite → Postgres |
| `src/bourse/models.py` | Shared `Listing` pydantic model used by all scrapers |
| `src/bourse/normalize.py` | `normalize_brand()` and `normalize_size()` — canonical mappings |
| `src/bourse/pg_report.py` | Postgres-backed report queries consumed by `GET /report` |
| `src/bourse/plick.py` | Plick.se scraper (HTML via httpx + selectolax) |
| `src/bourse/report.py` | SQLite-backed analytics + Rich terminal tables for the CLI |
| `src/bourse/scrape_tasks.py` | Background scrape tasks; pg helpers; watchlist run tracking; Postgres backoff |
| `src/bourse/seed_watchlist.py` | Seeds the Postgres watchlist with the curated 30-query arbitrage set |
| `src/bourse/tradera.py` | Tradera.se scraper (embedded JSON in search-page HTML, via httpx + selectolax) |
| `src/bourse/vinted.py` | Vinted.se scraper (JSON API via curl-cffi with Chrome TLS fingerprint) |
| `tests/test_alerts.py` | Tests for alerts.py (Codex owned) |
| `tests/test_blocket.py` | Blocket scraper unit tests (mocked `__NEXT_DATA__` fixtures) |
| `tests/test_compare.py` | Confidence tier + arbitrage signal unit tests |
| `tests/test_normalize.py` | 16 brand + 18 size normalization tests |

---

## Tech stack

- **Python 3.11+** for everything
- **httpx** — Plick, Tradera, and Blocket HTTP requests
- **curl-cffi** — Vinted requests (Chrome TLS fingerprint to pass Cloudflare); also the Blocket fallback when Cloudflare returns 403
- **selectolax** — HTML parsing for Plick, Tradera, and Blocket
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
DATABASE_URL=postgres://...       # Supabase Session Pooler connection string
TELEGRAM_BOT_TOKEN=...            # Telegram bot token (from BotFather)
TELEGRAM_CHAT_ID=...              # Your personal chat ID or group ID
```

`DATABASE_URL` is required for the API to start. `TELEGRAM_*` vars are optional — alerts are silently skipped if missing. The CLI uses SQLite and doesn't need any of these.

On Railway, all env vars are set in the Railway dashboard — `load_dotenv()` is a no-op there.

---

## Database schema

### SQLite (local CLI — `bourse.db`)

```sql
CREATE TABLE listings (
    listing_id    TEXT PRIMARY KEY,       -- "{platform}:{platform_id}"
    platform      TEXT NOT NULL,          -- plick | vinted | tradera
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    brand         TEXT,                   -- normalized via normalize_brand()
    size          TEXT,                   -- normalized via normalize_size()
    condition     TEXT,
    material      TEXT,
    seller_name   TEXT NOT NULL,
    seller_rating REAL,
    posted_at     TIMESTAMP,              -- Vinted exposes this; Plick/Tradera don't
    first_seen    TIMESTAMP NOT NULL,
    last_seen     TIMESTAMP NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',  -- active | sold | removed | unknown
    raw           TEXT,                   -- JSON blob for extra platform fields (e.g. Blocket location/category)
    image_url     TEXT                    -- thumbnail URL when scraper exposes one (Blocket today; others null until they populate)
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

CREATE TABLE price_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    query       TEXT,
    category    TEXT,
    size        TEXT,
    max_price   INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### Postgres (Supabase — API production)

Same schema as SQLite with these differences:
- `listing_snapshots.id` is `SERIAL PRIMARY KEY` (not `AUTOINCREMENT`)
- All timestamps stored as native `TIMESTAMP` (not ISO strings)
- `price_alerts` table lives in SQLite only for now (see known issues)
- `image_url` is added via `ensure_listing_columns()` idempotent migration at API startup

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

The scraping tasks (`run_scrape_new`, `run_scrape_update`) call `pg_read_watchlist()`, which reads from Postgres and falls back to `watchlist.txt`. If Postgres fails 3 consecutive times, `pg_read_watchlist()` enters a 10-minute backoff and reads from the file during that window (see Supabase circuit breaker section below).

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

## Telegram alerts

`alerts.py` checks every new listing scraped by `run_scrape_new()` against active `price_alerts` rows. A match fires a Telegram message.

**Rate limiting behaviour:**
- Max 5 messages per scrape run — top matches sorted by likes, overflow sent as `"...and X more matches"`
- 1-second sleep between each message
- 429 from Telegram → retry once after 5 seconds before giving up

**Matching logic:**
- `max_price` — listing price must be strictly below the alert threshold
- `category` — optional; matched via keyword detection on the title
- `size` — optional; exact string match on normalized size
- A listing only fires one message even if it matches multiple alerts

**CLI commands:**
```bash
bourse alerts test-telegram             # send a test message to verify credentials
bourse alerts list                      # list all active alerts
bourse alerts add "acne hoodie" --max-price 800 --category hoodie --size M
bourse alerts remove <id>               # deactivate by ID
```

**API endpoints** (`/alerts` prefix, via `alert_routes.py`):
```
GET    /alerts               → [{id, query, category, size, max_price, active}]
POST   /alerts               body: {query, max_price_sek, category?, size?} → 201
DELETE /alerts/{id}          → 200 or 404
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

bourse scrape tradera "acne studios"        # same for Tradera
bourse scrape tradera --watchlist

bourse scrape blocket "iphone 14 pro"       # same for Blocket
bourse scrape blocket --watchlist

bourse scrape all --watchlist               # all four platforms, all queries
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

### Alerts

```bash
bourse alerts test-telegram             # verify Telegram bot credentials
bourse alerts list                      # show all active price alerts
bourse alerts add "query" --max-price N [--category C] [--size S]
bourse alerts remove <id>
```

---

## API endpoints

Base URL locally: `http://localhost:8000`  
Base URL on Railway: `https://bourse-production.up.railway.app`

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
POST /scrape         → full scrape (all platforms, all queries, via subprocess)
POST /scrape/new     → new listings only (stops per query when page is all known IDs)
POST /scrape/update  → re-fetches all active listings to update price/likes/status
```

After `POST /scrape/new`, `watchlist_runs` is upserted per query with exact new-listing count.  
After `POST /scrape/update`, `watchlist_runs` is upserted for all queries with updated timestamp only.

### Listings

```
GET /listings
Query params (all optional):
  platform=plick|vinted|tradera
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

### Alerts

```
GET    /alerts               → [{id, query, category, size, max_price, active}]
POST   /alerts               {query, max_price_sek, category?, size?} → 201
DELETE /alerts/{id}          → 200 or 404
```

### Platforms (cross-platform comparison)

```
GET /platforms/compare?q=...
       &condition=...   (optional)
       &size=...        (optional)
       &brand=...       (optional)
       &days=90         (default 90, in 1..365)
→ {
    query: str,
    filters: {condition, size, brand, days},
    total_listings: int,
    platforms: [
      {
        platform,                  -- "plick" | "vinted" | "tradera" | "blocket"
        count,
        avg_price, median_price,
        p25, p75,                  -- quartiles for the spread bar
        min_price, max_price,
        avg_likes,                 -- null when no platform exposes likes (e.g. Blocket)
        avg_days_on_market,
        sample_confidence,         -- "low" <5, "medium" 5–15, "high" >15
        cheapest_3: [
          {id, title, url, image_url, condition, size, price_sek, likes}
        ]
      }
    ],
    arbitrage_signal: {            -- null when fewer than 2 platforms have sample ≥5
      buy_platform, sell_platform,
      buy_price_sek, sell_median_sek,
      est_margin_sek,              -- revenue (sell median × (1 − sell_fee)) − cost (cheapest_buy + shipping)
      est_margin_pct,
      gross_margin_sek             -- sell_median − cheapest_buy, before fees + shipping
    } | null
  }
```

Same keyword logic as `/listings/by-query`: AND match for ≥3 words, OR for 1–2.
Filters to active listings + sane prices + first_seen within `days`. Returns one
entry per platform with at least one match; the frontend can hide low-confidence
cells. Arbitrage signal evaluates all ordered pairs (up to 12 across 4 platforms)
and picks the highest net margin. Fees + shipping come from `src/bourse/fees.py`:

```python
PLATFORM_FEES = {"plick": 0.0, "vinted": 0.05, "tradera": 0.10, "blocket": 0.0}
SHIPPING_COST_SEK = 79
```

```
GET /platforms/compare/history?q=...&days=30
→ {
    query: str,
    days: int,
    platforms: [
      {platform, points: [{day: "YYYY-MM-DD", median_price, sample_size}]}
    ]
  }
```

Daily median prices grouped by date + platform from `listing_snapshots`. Powers
the 30-day spark line on each platform column in the redesigned `/compare` page.

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
   - **Tradera**: fetch HTML search pages via httpx, parse embedded `__NEXT_DATA__` JSON
   - **Blocket**: fetch HTML search pages via httpx → embedded `__NEXT_DATA__` JSON; auto-falls-back to curl-cffi if Cloudflare returns 403. Categories are filtered in the parser (electronics, fashion, sport, watches, photo); vehicles/real estate/jobs are dropped. "Skicka bud" / 0-SEK / auction listings are dropped. `likes` left null.
   - Stop scraping pages as soon as an entire page contains only known IDs
4. Filter results: keyword match + price sanity (200–50,000 SEK) + normalize brand/size
5. Upsert to `listings` + insert to `listing_snapshots` in Postgres
6. Upsert `watchlist_runs` for the query with timestamp + count
7. Call `check_alerts(all_new)` — fires Telegram messages for any matches (matches across all platforms including Blocket)

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
- Tradera: HTML cached to `.cache/tradera-{sha256[:20]}.html` for 24h
- Blocket: HTML cached to `.cache/blocket-{sha256[:20]}.html` for 24h
- `fetch_listing()` in all scrapers bypasses cache (always fetches live for updates)

### Rate limiting

| Platform | Delay | Backoff | Parser |
|----------|-------|---------|--------|
| Plick | 2–4s random between requests | 2s → 4s → 8s → abort | HTML via selectolax |
| Vinted | 1–2s random between requests | 2s → 4s → 8s → abort | JSON via curl-cffi |
| Tradera | 2–4s random between requests | 2s → 4s → 8s → abort | `__NEXT_DATA__` via httpx |
| Blocket | 2–4s random between requests | 2s → 4s → 8s → abort | `__NEXT_DATA__` via httpx, curl-cffi fallback on 403 |

Never parallel. Always single-threaded.

---

## Supabase circuit breaker and Postgres backoff

Supabase's connection pooler (pgBouncer) has a circuit breaker that trips when it receives too many failing connection attempts in a short window. This was triggered by Railway health checks calling `GET /watchlist` every minute while the DB was briefly unreachable, causing a flood of `pg_read_watchlist()` retries that kept the circuit open.

**Fix in `scrape_tasks.py`:** `pg_read_watchlist()` tracks consecutive failures with module-level state:

- `_pg_failure_count` — increments on each failure, resets to 0 on success
- `_pg_backoff_until` — set to `now + 10 minutes` when `_pg_failure_count` reaches 3; cleared on success

While `now < _pg_backoff_until`, all calls return immediately from `watchlist.txt` without touching Postgres. After the backoff window expires the next call probes Postgres again; one success fully resets the state.

**If Supabase's circuit breaker is already tripped on a fresh Railway deploy:** wait 5–10 minutes before triggering any scrape. The health check endpoint (`GET /watchlist`) will keep returning file-backed data during the backoff window, so Railway won't mark the service as down. Once the circuit resets, the first successful `pg_read_watchlist()` call clears the backoff automatically.

---

## Deployment

### Railway (production)

- **URL**: `https://bourse-production.up.railway.app`
- **Service**: FastAPI app via uvicorn
- **Start command**: `uvicorn bourse.api:app --host 0.0.0.0 --port $PORT`
- **Builder**: NIXPACKS — installs from `requirements.txt`, which includes `.` at the top to install the local `bourse` package
- **Health check**: `GET /watchlist` with 30s timeout
- **Restart policy**: ON_FAILURE
- **Config**: `railway.toml` + `Procfile` at project root

### What's on Railway

- The FastAPI API (`api.py`)
- All scraping endpoints (`/scrape`, `/scrape/new`, `/scrape/update`)
- All alert endpoints (`/alerts`)
- Postgres connection via `DATABASE_URL` env var

### What's still local only

- The `bourse` CLI (SQLite-backed)
- `bourse.db` (SQLite database, including `price_alerts` table)
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

- **`price_alerts` table is SQLite-only** — `alerts.py` reads from `bourse.db`, which is not persisted across Railway deploys. Alerts added via CLI survive locally; alerts added via the API (`/alerts`) are lost on redeploy. Fix: migrate `price_alerts` to Postgres.
- **Supabase circuit breaker** — if the DB was unreachable during a recent deploy, it may take 5–10 minutes for Supabase's circuit breaker to reset. During that window `pg_read_watchlist()` serves from `watchlist.txt`. Do not trigger scrapes until `GET /watchlist` returns Postgres-backed data (check `last_scraped_at` is non-null).
- **`bourse db clean` and `bourse db clean-prices`** — only work on SQLite (local); no Postgres equivalent yet
- **`migrate.py`** — one-time script with no idempotency for updates; running twice silently skips duplicates but doesn't update changed fields
- **Plick search results ordering** — "relevance" ordering shifts over time; `position_in_search` is approximate
- **Vinted `posted_at`** — comes from photo upload timestamp (a proxy, not the true listing date)

---

## Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| 1 | ✅ Done | Plick scraper, SQLite, CLI |
| 2 | ✅ Done | Vinted scraper, Postgres, FastAPI, Railway deploy, normalization |
| 2.5 | ✅ Done | Tradera scraper, fully wired into API and CLI (new + update flows) |
| 3 | ✅ Done | Telegram alerts, price alert CRUD, rate limiting, Postgres watchlist, backoff |
| 4 | Planned | Web UI (Lovable), scheduled scraping (cron on Railway), multi-user |

### Immediate next tasks

1. **Configure Railway cron services** — entry point `src/bourse/cron.py` is ready. Create two cron services in the Railway dashboard pointing at this repo, both running `python -m bourse.cron`: one with `CRON_JOB=scrape-new` daily, one with `CRON_JOB=scrape-update` every few hours. Both need `DATABASE_URL` (and `TELEGRAM_*` for alerts).
2. **`price_alerts` to Postgres** — `alerts.py` uses SQLite; alerts are lost on redeploy. Migrate the table and update `alerts.py` and `alert_routes.py` to use `pg_read()`/`pg_write()`.
3. **Postgres `clean-prices` command** — equivalent of `bourse db clean-prices` but targets the live Postgres DB

### Seeding the watchlist

`src/bourse/seed_watchlist.py` POSTs the 30 curated arbitrage queries to the
running API. Idempotent — queries already in `watchlist` are skipped:

```bash
# Local API on http://localhost:8000
python3 -m bourse.seed_watchlist

# Production
BOURSE_API_URL=https://bourse-production.up.railway.app python3 -m bourse.seed_watchlist
```

### Fees and shipping config

`src/bourse/fees.py` defines `PLATFORM_FEES` (Plick 0%, Vinted 5%, Tradera 10%,
Blocket 0%) and `SHIPPING_COST_SEK` (flat 79 kr). Edit this file to retune
the net arbitrage signal — values are read at request time, no restart needed.

---

## Note on AGENTS.md

`AGENTS.md` exists at the project root with the same content as `CLAUDE.md`. Keep both files in sync — Codex reads `AGENTS.md` and Claude Code reads `CLAUDE.md`.
