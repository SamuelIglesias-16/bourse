"""Bourse CLI — entry point for all commands."""

import logging
from pathlib import Path
from typing import Optional

import typer

from bourse.db import DB_PATH, get_connection, init_db
from bourse.ingest import ingest, PRICE_MIN, PRICE_MAX
from bourse.blocket import scrape_query as scrape_blocket
from bourse.ebay import scrape_query as scrape_ebay, scrape_query_ended as scrape_ebay_sold
from bourse.plick import scrape_query as scrape_plick
from bourse.tradera import scrape_query as scrape_tradera, scrape_query_ended as scrape_tradera_ended
from bourse.vinted import scrape_query as scrape_vinted
from bourse.report import print_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(help="Bourse — secondhand fashion market intelligence.")
scrape_app = typer.Typer(help="Scrape platforms.")
watchlist_app = typer.Typer(help="Manage the search watchlist.")
db_app = typer.Typer(help="Database utilities.")
alerts_app = typer.Typer(help="Manage price alerts.")
app.add_typer(scrape_app, name="scrape")
app.add_typer(watchlist_app, name="watchlist")
app.add_typer(db_app, name="db")
app.add_typer(alerts_app, name="alerts")

WATCHLIST_FILE = Path("watchlist.txt")


# ── Watchlist helpers ──────────────────────────────────────────────────────


def _read_watchlist() -> list[str]:
    if not WATCHLIST_FILE.exists():
        return []
    lines = WATCHLIST_FILE.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.startswith("#")]


def _write_watchlist(queries: list[str]) -> None:
    WATCHLIST_FILE.write_text(
        "\n".join(queries) + ("\n" if queries else ""), encoding="utf-8"
    )


# ── scrape plick ───────────────────────────────────────────────────────────


@scrape_app.command("plick")
def cmd_plick(
    query: Optional[str] = typer.Argument(
        None, help="Search query, e.g. 'mm gat'. Omit when using --watchlist."
    ),
    watchlist: bool = typer.Option(
        False, "--watchlist", help="Read queries from watchlist.txt and scrape all."
    ),
    pages: int = typer.Option(5, "--pages", help="Search result pages to fetch per query."),
) -> None:
    """Scrape Plick for one query or for every query in watchlist.txt."""
    if watchlist and query:
        typer.echo("Error: provide either a query argument or --watchlist, not both.", err=True)
        raise typer.Exit(code=1)
    if not watchlist and not query:
        typer.echo("Error: provide a query argument or --watchlist.", err=True)
        raise typer.Exit(code=1)

    queries = _read_watchlist() if watchlist else [query]  # type: ignore[list-item]

    if not queries:
        typer.echo("Watchlist is empty. Add queries with: bourse watchlist add \"query\"")
        raise typer.Exit()

    init_db()

    for q in queries:
        typer.echo(f"\nScraping Plick for: {q!r}")
        listings = scrape_plick(q, pages=pages)

        if not listings:
            typer.echo("  No listings found.")
            continue

        summary = ingest(listings, query=q)
        typer.echo(str(summary))


# ── scrape vinted ─────────────────────────────────────────────────────────


@scrape_app.command("vinted")
def scrape_vinted_cmd(
    query: Optional[str] = typer.Argument(
        None, help="Search query, e.g. 'acne studios'. Omit when using --watchlist."
    ),
    watchlist: bool = typer.Option(
        False, "--watchlist", help="Read queries from watchlist.txt and scrape all."
    ),
    pages: int = typer.Option(5, "--pages", help="Search result pages to fetch per query."),
) -> None:
    """Scrape Vinted for one query or for every query in watchlist.txt."""
    if watchlist and query:
        typer.echo("Error: provide either a query argument or --watchlist, not both.", err=True)
        raise typer.Exit(code=1)
    if not watchlist and not query:
        typer.echo("Error: provide a query argument or --watchlist.", err=True)
        raise typer.Exit(code=1)

    queries = _read_watchlist() if watchlist else [query]  # type: ignore[list-item]

    if not queries:
        typer.echo("Watchlist is empty. Add queries with: bourse watchlist add \"query\"")
        raise typer.Exit()

    init_db()

    for q in queries:
        typer.echo(f"\nScraping Vinted for: {q!r}")
        listings = scrape_vinted(q, pages=pages)

        if not listings:
            typer.echo("  No listings found.")
            continue

        summary = ingest(listings, query=q)
        typer.echo(str(summary))


# ── scrape tradera ────────────────────────────────────────────────────────


@scrape_app.command("tradera")
def scrape_tradera_cmd(
    query: Optional[str] = typer.Argument(
        None, help="Search query, e.g. 'acne studios'. Omit when using --watchlist."
    ),
    watchlist: bool = typer.Option(
        False, "--watchlist", help="Read queries from watchlist.txt and scrape all."
    ),
    pages: int = typer.Option(5, "--pages", help="Search result pages to fetch per query."),
) -> None:
    """Scrape Tradera for one query or for every query in watchlist.txt."""
    if watchlist and query:
        typer.echo("Error: provide either a query argument or --watchlist, not both.", err=True)
        raise typer.Exit(code=1)
    if not watchlist and not query:
        typer.echo("Error: provide a query argument or --watchlist.", err=True)
        raise typer.Exit(code=1)

    queries = _read_watchlist() if watchlist else [query]  # type: ignore[list-item]

    if not queries:
        typer.echo("Watchlist is empty. Add queries with: bourse watchlist add \"query\"")
        raise typer.Exit()

    init_db()

    for q in queries:
        typer.echo(f"\nScraping Tradera for: {q!r}")
        listings = scrape_tradera(q, pages=pages)

        if not listings:
            typer.echo("  No listings found.")
            continue

        summary = ingest(listings, query=q)
        typer.echo(str(summary))


# ── scrape blocket ────────────────────────────────────────────────────────


@scrape_app.command("blocket")
def scrape_blocket_cmd(
    query: Optional[str] = typer.Argument(
        None, help="Search query, e.g. 'iphone 14 pro'. Omit when using --watchlist."
    ),
    watchlist: bool = typer.Option(
        False, "--watchlist", help="Read queries from watchlist.txt and scrape all."
    ),
    pages: int = typer.Option(5, "--pages", help="Search result pages to fetch per query."),
) -> None:
    """Scrape Blocket for one query or for every query in watchlist.txt."""
    if watchlist and query:
        typer.echo("Error: provide either a query argument or --watchlist, not both.", err=True)
        raise typer.Exit(code=1)
    if not watchlist and not query:
        typer.echo("Error: provide a query argument or --watchlist.", err=True)
        raise typer.Exit(code=1)

    queries = _read_watchlist() if watchlist else [query]  # type: ignore[list-item]

    if not queries:
        typer.echo("Watchlist is empty. Add queries with: bourse watchlist add \"query\"")
        raise typer.Exit()

    init_db()

    for q in queries:
        typer.echo(f"\nScraping Blocket for: {q!r}")
        listings = scrape_blocket(q, pages=pages)

        if not listings:
            typer.echo("  No listings found.")
            continue

        summary = ingest(listings, query=q)
        typer.echo(str(summary))


# ── scrape ebay ────────────────────────────────────────────────────────────


@scrape_app.command("ebay")
def scrape_ebay_cmd(
    query: Optional[str] = typer.Argument(
        None, help="Search query, e.g. 'iphone 14 pro'. Omit when using --watchlist."
    ),
    watchlist: bool = typer.Option(
        False, "--watchlist", help="Read queries from watchlist.txt and scrape all."
    ),
    pages: int = typer.Option(3, "--pages", help="Search result pages to fetch per query."),
) -> None:
    """Scrape eBay (active listings) for one query or for every watchlist query."""
    if watchlist and query:
        typer.echo("Error: provide either a query argument or --watchlist, not both.", err=True)
        raise typer.Exit(code=1)
    if not watchlist and not query:
        typer.echo("Error: provide a query argument or --watchlist.", err=True)
        raise typer.Exit(code=1)

    queries = _read_watchlist() if watchlist else [query]  # type: ignore[list-item]
    if not queries:
        typer.echo("Watchlist is empty. Add queries with: bourse watchlist add \"query\"")
        raise typer.Exit()

    init_db()
    for q in queries:
        typer.echo(f"\nScraping eBay for: {q!r}")
        listings = scrape_ebay(q, pages=pages)
        if not listings:
            typer.echo("  No listings found.")
            continue
        summary = ingest(listings, query=q)
        typer.echo(str(summary))


# ── scrape sold (Plick Såld is caught by scrape/update; this covers Tradera + eBay) ─


@scrape_app.command("sold")
def scrape_sold_cmd(
    watchlist: bool = typer.Option(
        True, "--watchlist/--no-watchlist",
        help="Read queries from watchlist.txt (default: on).",
    ),
    query: Optional[str] = typer.Argument(None, help="Single query (omit when using --watchlist)."),
    pages: int = typer.Option(3, "--pages", help="Search pages to fetch per query per platform."),
) -> None:
    """Scrape SOLD prices across Tradera (ended auctions) and eBay (LH_Sold)."""
    if not watchlist and not query:
        typer.echo("Error: provide a query argument or --watchlist.", err=True)
        raise typer.Exit(code=1)
    queries = _read_watchlist() if watchlist else [query]  # type: ignore[list-item]
    if not queries:
        typer.echo("Watchlist is empty.")
        raise typer.Exit()
    init_db()
    for q in queries:
        typer.echo(f"\n── SOLD {q!r} ──")
        for label, fn in (("Tradera (ended)", scrape_tradera_ended), ("eBay (sold)", scrape_ebay_sold)):
            typer.echo(f"\n  Scraping {label}…")
            sold = fn(q, pages=pages)
            if not sold:
                typer.echo("    No sold listings found.")
                continue
            summary = ingest(sold, query=q)
            for line in str(summary).splitlines():
                typer.echo(f"  {line}")


# ── scrape all (all platforms) ─────────────────────────────────────────────


@scrape_app.command("all")
def scrape_all(
    watchlist: bool = typer.Option(
        True, "--watchlist/--no-watchlist",
        help="Read queries from watchlist.txt (default: on).",
    ),
    pages: int = typer.Option(5, "--pages", help="Search result pages to fetch per query per platform."),
) -> None:
    """Scrape Plick, Vinted, Tradera, Blocket and eBay for every query in watchlist.txt."""
    queries = _read_watchlist()
    if not queries:
        typer.echo(
            "Watchlist is empty. Add queries with: bourse watchlist add \"query\"",
            err=True,
        )
        raise typer.Exit(code=1)

    init_db()

    for q in queries:
        typer.echo(f"\n── {q!r} ──")
        for platform_name, scraper in [
            ("Plick", scrape_plick),
            ("Vinted", scrape_vinted),
            ("Tradera", scrape_tradera),
            ("Blocket", scrape_blocket),
            ("eBay", scrape_ebay),
        ]:
            typer.echo(f"\n  Scraping {platform_name}…")
            listings = scraper(q, pages=pages)
            if not listings:
                typer.echo("    No listings found.")
                continue
            summary = ingest(listings, query=q)
            for line in str(summary).splitlines():
                typer.echo(f"  {line}")


# ── watchlist add / remove ─────────────────────────────────────────────────


@watchlist_app.command("add")
def watchlist_add(
    query: str = typer.Argument(..., help="Search query to add to watchlist.txt."),
) -> None:
    """Add a query to watchlist.txt (no-op if already present)."""
    queries = _read_watchlist()
    if query in queries:
        typer.echo(f"Already in watchlist: {query!r}")
        return
    queries.append(query)
    _write_watchlist(queries)
    typer.echo(f"Added: {query!r}  ({len(queries)} queries total)")


@watchlist_app.command("remove")
def watchlist_remove(
    query: str = typer.Argument(..., help="Search query to remove from watchlist.txt."),
) -> None:
    """Remove a query from watchlist.txt."""
    queries = _read_watchlist()
    if query not in queries:
        typer.echo(f"Not found in watchlist: {query!r}", err=True)
        raise typer.Exit(code=1)
    queries.remove(query)
    _write_watchlist(queries)
    typer.echo(f"Removed: {query!r}  ({len(queries)} queries remaining)")


# ── report ─────────────────────────────────────────────────────────────────


@app.command("report")
def report(
    full: bool = typer.Option(False, "--full", help="Show all rows instead of top 10 per section."),
) -> None:
    """Print a category price summary and top-10 most-liked listings."""
    print_report(full=full)


# ── db clean ───────────────────────────────────────────────────────────────


@db_app.command("clean")
def db_clean() -> None:
    """Delete junk listings: no brand AND title matches no watchlist keyword."""
    watch_queries = _read_watchlist()
    if not watch_queries:
        typer.echo(
            "Watchlist is empty — refusing to clean without keywords to match against.\n"
            "Add queries first: bourse watchlist add \"query\"",
            err=True,
        )
        raise typer.Exit(code=1)

    # Flatten all watchlist queries into a deduplicated keyword set
    keywords = list({kw.lower() for q in watch_queries for kw in q.split() if kw})

    with get_connection(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT listing_id, title FROM listings WHERE brand IS NULL OR TRIM(brand) = ''"
        ).fetchall()

    # Keep only rows whose title contains none of the watchlist keywords
    junk = [r for r in rows if not any(kw in r["title"].lower() for kw in keywords)]

    if not junk:
        typer.echo("Nothing to clean — no junk listings found.")
        return

    typer.echo(f"Found {len(junk)} junk listing(s) to delete:")
    for r in junk[:8]:
        typer.echo(f"  {r['listing_id']}  {r['title'][:60]}")
    if len(junk) > 8:
        typer.echo(f"  … and {len(junk) - 8} more")

    typer.confirm(f"\nDelete {len(junk)} listings and all their snapshots?", abort=True)

    ids = [r["listing_id"] for r in junk]
    ph = ",".join("?" * len(ids))
    with get_connection(DB_PATH) as conn:
        conn.execute(f"DELETE FROM listing_snapshots WHERE listing_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM listings WHERE listing_id IN ({ph})", ids)

    typer.echo(f"Deleted {len(junk)} listings and their snapshots.")


@db_app.command("clean-prices")
def db_clean_prices() -> None:
    """Remove listings whose latest snapshot price is below 200 or above 50 000 SEK."""
    with get_connection(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT l.listing_id, l.title, s.price_sek
              FROM listings l
              JOIN listing_snapshots s ON s.id = (
                  SELECT id FROM listing_snapshots
                   WHERE listing_id = l.listing_id
                   ORDER BY scraped_at DESC LIMIT 1
              )
             WHERE s.price_sek < ? OR s.price_sek > ?
            """,
            (PRICE_MIN, PRICE_MAX),
        ).fetchall()

    if not rows:
        typer.echo(f"No listings found with price outside {PRICE_MIN}–{PRICE_MAX:,} SEK.")
        return

    typer.echo(f"Found {len(rows)} listing(s) with price outside {PRICE_MIN}–{PRICE_MAX:,} SEK:")
    for r in rows[:10]:
        typer.echo(f"  {r['listing_id']}  {r['price_sek']:>7,} SEK  {r['title'][:50]}")
    if len(rows) > 10:
        typer.echo(f"  … and {len(rows) - 10} more")

    typer.confirm(f"\nDelete {len(rows)} listing(s) and all their snapshots?", abort=True)

    ids = [r["listing_id"] for r in rows]
    ph = ",".join("?" * len(ids))
    with get_connection(DB_PATH) as conn:
        conn.execute(f"DELETE FROM listing_snapshots WHERE listing_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM listings WHERE listing_id IN ({ph})", ids)

    typer.echo(f"Deleted {len(rows)} listings and their snapshots.")


# ── alerts ────────────────────────────────────────────────────────────────────


@alerts_app.command("test-telegram")
def alerts_test_telegram() -> None:
    """Send a test message to Telegram to verify bot credentials."""
    from bourse.alerts import send_telegram
    send_telegram("🤖 Bourse bot is connected and working!")
    typer.echo("Test message sent.")


@alerts_app.command("list")
def alerts_list() -> None:
    """List all active price alerts."""
    from bourse.alerts import _ensure_price_alerts_table
    _ensure_price_alerts_table()
    with get_connection(DB_PATH) as conn:
        try:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN query TEXT")
        except Exception:
            pass
        rows = conn.execute(
            "SELECT id, query, category, size, max_price FROM price_alerts"
            " WHERE active = 1 ORDER BY id"
        ).fetchall()
    if not rows:
        typer.echo("No active alerts.")
        return
    for r in rows:
        d = dict(r)
        parts = [f"#{d['id']}"]
        if d.get("query"):
            parts.append(repr(d["query"]))
        parts.append(f"max {d['max_price']:,} SEK")
        if d.get("category"):
            parts.append(d["category"])
        if d.get("size"):
            parts.append(f"size {d['size']}")
        typer.echo("  ".join(parts))


@alerts_app.command("add")
def alerts_add(
    query: str = typer.Argument(..., help="Search query this alert covers."),
    max_price: int = typer.Option(..., "--max-price", help="Fire when price is below this (SEK)."),
    category: Optional[str] = typer.Option(None, "--category", help="Category filter (hoodie, jeans…)."),
    size: Optional[str] = typer.Option(None, "--size", help="Size filter (e.g. M, L, 42)."),
) -> None:
    """Add a new price alert."""
    from bourse.alerts import _ensure_price_alerts_table
    _ensure_price_alerts_table()
    with get_connection(DB_PATH) as conn:
        try:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN query TEXT")
        except Exception:
            pass
        conn.execute(
            "INSERT INTO price_alerts (query, category, size, max_price) VALUES (?, ?, ?, ?)",
            (query, category, size, max_price),
        )
    label = f"{query!r}  max {max_price:,} SEK"
    if category:
        label += f"  category={category}"
    if size:
        label += f"  size={size}"
    typer.echo(f"Alert added: {label}")


@alerts_app.command("remove")
def alerts_remove(
    alert_id: int = typer.Argument(..., help="ID of the alert to deactivate."),
) -> None:
    """Deactivate a price alert by ID."""
    from bourse.alerts import _ensure_price_alerts_table
    _ensure_price_alerts_table()
    with get_connection(DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE price_alerts SET active = 0 WHERE id = ? AND active = 1",
            (alert_id,),
        )
        affected = cur.rowcount
    if affected == 0:
        typer.echo(f"Alert #{alert_id} not found or already inactive.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Alert #{alert_id} deactivated.")


@db_app.command("backfill-brands")
def db_backfill_brands() -> None:
    """Normalize brand field for all listings in Postgres."""
    from bourse.backfill import backfill_brands
    updated = backfill_brands()
    typer.echo(f"Updated {updated} listing(s) with normalized brand values.")


@db_app.command("backfill-sizes")
def db_backfill_sizes() -> None:
    """Normalize size field for all listings in Postgres."""
    from bourse.backfill import backfill_sizes
    updated = backfill_sizes()
    typer.echo(f"Updated {updated} listing(s) with normalized size values.")
