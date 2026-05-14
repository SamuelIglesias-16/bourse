# Session notes — handoff for next Claude session

**Last session ended:** 2026-05-12. Working state: everything deployed and functional.

---

## Where we left off

Just finished building **cross-platform price comparison** end-to-end (backend endpoint + frontend page + nav). Railway deploy was briefly broken by a missing `.` in `requirements.txt`; Codex PR #1 fixed it via `buildCommand` in `railway.toml`. Deploy is now succeeding and the `/compare` page is live.

The user is **non-technical (18 y/o)** and works across two repos:
- `~/projects/bourse` — FastAPI backend (this repo, where next session starts)
- `~/projects/garms-glance` — React frontend (Lovable export, fully redesigned in this session)

---

## What's new this session

### Backend (`~/projects/bourse`)

- **New endpoint: `GET /platforms/compare?q=...`** — groups active listings by platform, returns count / avg / median / min / max per platform. Hides cells with <3 listings. Uses the same AND/OR keyword logic as `/listings/by-query` (AND for ≥3 words, OR otherwise). See `src/bourse/api.py:380`.
- Requires the `.` line in `requirements.txt` OR the `buildCommand = "pip install ."` in `railway.toml` (the latter is Codex's fix and is what's currently in place).

### Frontend (`~/projects/garms-glance`)

Fully redesigned from the original Lovable scaffold:

- **Design system rewritten** (`src/styles.css`) — light editorial theme (warm cream bg, near-white cards, deep terracotta accent `oklch(0.52 0.13 38)`), Manrope + JetBrains Mono type stack, dot-grid + accent halo backgrounds, paper-grain noise overlay, six purposeful motion primitives.
- **AppShell rewritten** (`src/components/AppShell.tsx`) — slim full-height left rail with 7 nav items (Dashboard, Watchlist, Items, Listings, **Compare**, Analytics, Alerts), thin accent line for active route, live freshness dot at bottom.
- **Dashboard fixed** (`src/routes/index.tsx`) — KPIs now use `/stats` for accurate numbers (active price drops was 0 due to /report not returning the field; now shows real 73+). Removed hardcoded fake deltas like `"+18 vs avg"`. Removed AI brief card.
- **Landing page rebuilt** (`src/routes/landing.tsx`) — hoodie centerpiece spinning at 24fps via `<HoodieSpinner>` canvas component that cycles through 169 transparent WebP frames (`public/frames/f_*.webp`). Background was removed via `rembg` (Python ML). Cursor-tracking 3D tilt, terracotta halo, 6 nav cards orbiting around the hoodie.
- **New page: `/compare`** (`src/routes/compare.tsx`) — search input + 3 platform cards showing median/avg/range/spread + insight banner ("X medians ~N% higher than Y"). Works for fashion AND any other product type that's in the DB.

### Infrastructure

- `src/bourse/cron.py` exists (entry point for scheduled scrapes) but **schedules still aren't configured in the Railway dashboard.** That's the next infrastructure task.

---

## Current state

| Thing | Status |
|---|---|
| Railway API | ✅ Working (`https://bourse-production.up.railway.app`) |
| `/platforms/compare` endpoint | ✅ Deployed |
| `/compare` frontend page | ✅ Live |
| Dashboard KPI numbers | ✅ Accurate (from `/stats`) |
| Landing page hoodie animation | ✅ Working (24fps, transparent canvas) |
| Railway cron services | ❌ Not configured — `cron.py` exists but no schedule |
| `price_alerts` table | ❌ Still SQLite-only — lost on Railway redeploy |
| Scraper coverage for non-fashion | ⚠️ Untested — Plick/Vinted are fashion-filtered; Tradera should work for electronics |

---

## Open / next tasks (priority order)

1. **Configure Railway cron services** so `cron.py` actually runs daily. Two services in the Railway dashboard pointing at this repo: `CRON_JOB=scrape-new python -m bourse.cron` daily, `CRON_JOB=scrape-update python -m bourse.cron` every few hours. Both need `DATABASE_URL` and `TELEGRAM_*` env vars.
2. **Migrate `price_alerts` from SQLite to Postgres** so alerts created via the API survive redeploys.
3. **Verify scraper coverage for non-fashion queries** — test by adding "rtx 4070" to watchlist and scraping. If Plick/Vinted return nothing, the scraper URLs need adjusting (Tradera should already work — it's a general marketplace).
4. **Real deltas on dashboard KPIs** — currently no deltas shown. Would require a daily `kpi_snapshots` table written by the cron service.

---

## How to verify things if anything looks broken

```bash
# Backend live + has the compare endpoint
curl -s "https://bourse-production.up.railway.app/stats" | head -1
curl -s "https://bourse-production.up.railway.app/platforms/compare?q=acne+studios" | head -3

# Frontend dev server (run in ~/projects/garms-glance)
bun dev   # opens at http://localhost:8080
```

---

## Local git state at session end

`~/projects/bourse` had a few uncommitted modifications at session end (`alerts.py`, `scrape_tasks.py`, `cron.py`, `AGENTS.md`, `CLAUDE.md`). These are unrelated to the Compare feature work — they were already uncommitted when this session began. Decide whether to commit / discard them when convenient.

`~/projects/garms-glance` likely has a lot of uncommitted work too — the entire redesign + Compare page were not committed during this session. Worth reviewing and committing in chunks.
