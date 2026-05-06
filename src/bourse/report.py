"""Analytics report — category stats and top-liked listings."""

import polars as pl
from rich import box
from rich.console import Console
from rich.table import Table

from bourse.db import DB_PATH, get_connection

console = Console()

# Swedish + English keywords for each category
CATEGORIES: dict[str, list[str]] = {
    "hoodie":      ["hoodie", "huvtröja", "huvtrojor", "sweatshirt"],
    "longsleeve":  ["longsleeve", "long sleeve", "långärmad", "langarmad"],
    "t-shirt":     ["t-shirt", "tshirt", "t shirt"],
    "jeans":       ["jeans", "denim"],
    "jacket":      ["jacka", "jacket", "kappa", "coat", "väst"],
}


def _detect_category(title: str) -> str:
    t = title.lower()
    for name, keywords in CATEGORIES.items():
        if any(kw in t for kw in keywords):
            return name
    return "other"


def _vel_str(delta: int) -> str:
    """Rich-markup string for a like-velocity delta."""
    if delta <= 0:
        return "[dim]+0[/dim]"
    if delta >= 5:
        return f"[bold green]+{delta}[/bold green]"
    return f"[green]+{delta}[/green]"


def _dom_style(days: int) -> str:
    """Rich colour tag for a days-on-market value."""
    if days >= 30:
        return "bold red"
    if days >= 14:
        return "dark_orange"
    return "yellow"


def _st_style(pct: float) -> str:
    """Rich colour tag for a sell-through percentage."""
    if pct >= 50:
        return "bold green"
    if pct >= 25:
        return "green"
    return "yellow"


# ── Data loaders ───────────────────────────────────────────────────────────


def _load_df() -> pl.DataFrame:
    """One row per active listing: latest price, likes, DOM, and like velocity."""
    with get_connection(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT l.listing_id,
                   l.title,
                   COALESCE(l.brand, '—')                                       AS brand,
                   s.price_sek,
                   COALESCE(s.likes, 0)                                         AS likes,
                   CAST(julianday('now') - julianday(l.first_seen) AS INTEGER)  AS days_on_market,
                   COALESCE(s.likes, 0) - COALESCE(s0.likes, 0)                AS like_velocity
              FROM listings l
              JOIN listing_snapshots s ON s.id = (
                  SELECT id FROM listing_snapshots
                   WHERE listing_id = l.listing_id
                   ORDER BY scraped_at DESC LIMIT 1
              )
              JOIN listing_snapshots s0 ON s0.id = (
                  SELECT id FROM listing_snapshots
                   WHERE listing_id = l.listing_id
                   ORDER BY scraped_at ASC LIMIT 1
              )
             WHERE l.status = 'active'
        """).fetchall()

    if not rows:
        return pl.DataFrame()
    return pl.from_dicts([dict(r) for r in rows])


def _load_sold_df() -> pl.DataFrame:
    """One row per sold/removed listing: final price, final likes, DOM (first→last seen)."""
    with get_connection(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT l.listing_id,
                   l.title,
                   COALESCE(l.brand, '—')                                                   AS brand,
                   l.status,
                   s.price_sek                                                              AS final_price,
                   COALESCE(s.likes, 0)                                                     AS final_likes,
                   CAST(julianday(l.last_seen) - julianday(l.first_seen) AS INTEGER)        AS days_on_market
              FROM listings l
              JOIN listing_snapshots s ON s.id = (
                  SELECT id FROM listing_snapshots
                   WHERE listing_id = l.listing_id
                   ORDER BY scraped_at DESC LIMIT 1
              )
             WHERE l.status IN ('sold', 'removed')
             ORDER BY l.last_seen DESC
        """).fetchall()

    if not rows:
        return pl.DataFrame()
    return pl.from_dicts([dict(r) for r in rows])


def _load_price_drops_df() -> pl.DataFrame:
    """Active listings (≥2 snapshots) where the latest price is lower than the first."""
    with get_connection(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT l.listing_id,
                   l.title,
                   l.platform,
                   COALESCE(l.brand, '—')                                      AS brand,
                   s0.price_sek                                                 AS original_price,
                   s.price_sek                                                  AS current_price,
                   CAST(julianday('now') - julianday(l.first_seen) AS INTEGER)  AS days_on_market,
                   COALESCE(s.likes, 0)                                         AS likes,
                   COUNT(snap.id)                                               AS snapshot_count
              FROM listings l
              JOIN listing_snapshots s ON s.id = (
                  SELECT id FROM listing_snapshots
                   WHERE listing_id = l.listing_id
                   ORDER BY scraped_at DESC LIMIT 1
              )
              JOIN listing_snapshots s0 ON s0.id = (
                  SELECT id FROM listing_snapshots
                   WHERE listing_id = l.listing_id
                   ORDER BY scraped_at ASC LIMIT 1
              )
              JOIN listing_snapshots snap ON snap.listing_id = l.listing_id
             WHERE l.status = 'active'
               AND s.price_sek < s0.price_sek
             GROUP BY l.listing_id
            HAVING snapshot_count >= 2
        """).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.from_dicts([dict(r) for r in rows])


def _load_brand_sellthrough_df() -> pl.DataFrame:
    """Per-brand sell-through rate (brands with ≥5 total listings)."""
    with get_connection(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT l.brand,
                   COUNT(*)                                                             AS total,
                   SUM(CASE WHEN l.status IN ('sold', 'removed') THEN 1 ELSE 0 END)   AS sold_count,
                   AVG(CASE WHEN l.status IN ('sold', 'removed')
                       THEN CAST(julianday(l.last_seen) - julianday(l.first_seen) AS INTEGER)
                       ELSE NULL END)                                                   AS avg_dom_sold
              FROM listings l
             WHERE l.brand IS NOT NULL AND TRIM(l.brand) != ''
             GROUP BY l.brand
            HAVING COUNT(*) >= 5
             ORDER BY (CAST(sold_count AS REAL) / COUNT(*)) DESC
        """).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.from_dicts([dict(r) for r in rows])


def _load_day_df() -> pl.DataFrame:
    """Sell-through stats by listing day-of-week (posted_at when available, else first_seen)."""
    with get_connection(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT CAST(strftime('%w', COALESCE(l.posted_at, l.first_seen)) AS INTEGER) AS day_num,
                   COUNT(*)                                                               AS total,
                   SUM(CASE WHEN l.status IN ('sold', 'removed') THEN 1 ELSE 0 END)     AS sold_count,
                   AVG(CASE WHEN l.status IN ('sold', 'removed')
                       THEN CAST(julianday(l.last_seen) - julianday(l.first_seen) AS INTEGER)
                       ELSE NULL END)                                                     AS avg_dom_sold
              FROM listings l
             GROUP BY day_num
             ORDER BY day_num
        """).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.from_dicts([dict(r) for r in rows])


# ── Report entry point ─────────────────────────────────────────────────────


def print_report(full: bool = False) -> None:
    """Print the full analytics report to the terminal."""
    df = _load_df()

    if df.is_empty():
        console.print("\n[yellow]No active listings in the database yet.[/yellow]\n")
        return

    df = df.with_columns(
        pl.col("title").map_elements(_detect_category, return_dtype=pl.Utf8).alias("category")
    )

    console.print()
    _print_category_table(df)
    console.print()
    _print_top_liked(df)
    console.print()
    _print_sold()
    console.print()
    _print_stale(df)
    console.print()
    _print_price_drops(full=full)
    console.print()
    _print_brand_sellthrough(full=full)
    console.print()
    _print_best_day()
    console.print()


# ── Table printers ─────────────────────────────────────────────────────────


def _print_category_table(df: pl.DataFrame) -> None:
    stats = (
        df.group_by("category")
        .agg(
            pl.col("listing_id").count().alias("count"),
            pl.col("price_sek").mean().round(0).cast(pl.Int64).alias("avg_price"),
            pl.col("price_sek").median().round(0).cast(pl.Int64).alias("median_price"),
            pl.col("likes").max().alias("max_likes"),
            pl.col("days_on_market").mean().round(0).cast(pl.Int64).alias("avg_dom"),
        )
        .sort("count", descending=True)
    )

    table = Table(
        title="Listings by Category",
        box=box.ROUNDED, show_lines=True,
        header_style="bold white on dark_blue", title_style="bold cyan",
    )
    table.add_column("Category",     style="cyan bold", min_width=12)
    table.add_column("Listings",     justify="right",   style="white")
    table.add_column("Avg Price",    justify="right",   style="green")
    table.add_column("Median Price", justify="right",   style="green")
    table.add_column("Top Likes",    justify="right",   style="magenta")
    table.add_column("Avg DOM",      justify="right",   style="yellow")

    for row in stats.iter_rows(named=True):
        table.add_row(
            row["category"],
            str(row["count"]),
            f"{row['avg_price']:,} SEK",
            f"{row['median_price']:,} SEK",
            str(row["max_likes"] or 0),
            f"{row['avg_dom']}d",
        )
    console.print(table)


def _print_top_liked(df: pl.DataFrame) -> None:
    top = df.sort("likes", descending=True).head(10)

    table = Table(
        title="Top 10 Most Liked",
        box=box.ROUNDED, show_lines=True,
        header_style="bold white on dark_blue", title_style="bold cyan",
    )
    table.add_column("#",        justify="right", style="dim",         width=3)
    table.add_column("Title",    style="white",                        max_width=34)
    table.add_column("Brand",    style="cyan",                         max_width=14)
    table.add_column("Category", style="dim")
    table.add_column("Price",    justify="right", style="green")
    table.add_column("Likes",    justify="right", style="bold magenta")
    table.add_column("Vel.",     justify="right")
    table.add_column("DOM",      justify="right", style="yellow")

    for i, row in enumerate(top.iter_rows(named=True), 1):
        table.add_row(
            str(i),
            row["title"],
            row["brand"],
            row["category"],
            f"{row['price_sek']:,} SEK",
            str(row["likes"]),
            _vel_str(row["like_velocity"]),
            f"{row['days_on_market']}d",
        )
    console.print(table)


def _print_sold() -> None:
    df = _load_sold_df()

    if df.is_empty():
        console.print("[dim]No sold or removed listings recorded yet.[/dim]")
        return

    table = Table(
        title=f"Sold / Removed  [dim]({len(df)} listings)[/dim]",
        box=box.ROUNDED, show_lines=True,
        header_style="bold white on dark_blue", title_style="bold green",
    )
    table.add_column("Title",       style="white",          max_width=34)
    table.add_column("Brand",       style="cyan",           max_width=14)
    table.add_column("Category",    style="dim")
    table.add_column("Status",      justify="center")
    table.add_column("Final Price", justify="right",        style="green")
    table.add_column("Likes",       justify="right",        style="magenta")
    table.add_column("DOM",         justify="right")

    for row in df.iter_rows(named=True):
        dom = row["days_on_market"]
        status = row["status"]
        status_markup = "[green]sold[/green]" if status == "sold" else "[dim]removed[/dim]"
        table.add_row(
            row["title"],
            row["brand"],
            _detect_category(row["title"]),
            status_markup,
            f"{row['final_price']:,} SEK",
            str(row["final_likes"]),
            f"[{_dom_style(dom)}]{dom}d[/{_dom_style(dom)}]",
        )
    console.print(table)


def _print_stale(df: pl.DataFrame) -> None:
    stale = (
        df.filter((pl.col("days_on_market") > 7) & (pl.col("likes") < 5))
        .sort("days_on_market", descending=True)
    )

    if stale.is_empty():
        console.print("[green]✓ No stale listings (all active items are <7 days old or have ≥5 likes).[/green]")
        return

    table = Table(
        title=f"Stale Listings  [dim]({len(stale)} items · >7 days on market · <5 likes)[/dim]",
        box=box.ROUNDED, show_lines=True,
        header_style="bold white on dark_blue", title_style="bold red",
    )
    table.add_column("Title",  style="white",  max_width=38)
    table.add_column("Brand",  style="cyan",   max_width=14)
    table.add_column("Price",  justify="right", style="green")
    table.add_column("Likes",  justify="right", style="magenta")
    table.add_column("Vel.",   justify="right")
    table.add_column("DOM",    justify="right")

    for row in stale.iter_rows(named=True):
        dom = row["days_on_market"]
        table.add_row(
            row["title"],
            row["brand"],
            f"{row['price_sek']:,} SEK",
            str(row["likes"]),
            _vel_str(row["like_velocity"]),
            f"[{_dom_style(dom)}]{dom}d[/{_dom_style(dom)}]",
        )
    console.print(table)


def _print_price_drops(full: bool = False) -> None:
    df = _load_price_drops_df()
    if df.is_empty():
        console.print("[dim]No price drops recorded yet (need ≥2 snapshots per listing).[/dim]")
        return

    df = df.with_columns([
        (pl.col("original_price") - pl.col("current_price")).alias("drop_amount"),
        ((pl.col("original_price") - pl.col("current_price")) * 100.0 / pl.col("original_price"))
            .round(1).alias("drop_pct"),
    ]).sort("drop_pct", descending=True)

    total = len(df)
    display = df if full else df.head(10)
    note = f"{total} listings" + (f" · showing top {len(display)}" if not full and total > 10 else "")

    table = Table(
        title=f"Price Drops  [dim]({note})[/dim]",
        box=box.ROUNDED, show_lines=True,
        header_style="bold white on dark_blue", title_style="bold cyan",
    )
    table.add_column("Title",    style="white",   max_width=34)
    table.add_column("Platform", style="dim",     max_width=8)
    table.add_column("Was",      justify="right", style="dim")
    table.add_column("Now",      justify="right", style="green")
    table.add_column("Drop",     justify="right", style="bold red")
    table.add_column("Drop %",   justify="right", style="bold red")
    table.add_column("Likes",    justify="right", style="magenta")
    table.add_column("DOM",      justify="right")

    for row in display.iter_rows(named=True):
        dom = row["days_on_market"]
        table.add_row(
            row["title"],
            row["platform"],
            f"{row['original_price']:,} kr",
            f"{row['current_price']:,} kr",
            f"−{row['drop_amount']:,} kr",
            f"−{row['drop_pct']:.1f}%",
            str(row["likes"]),
            f"[{_dom_style(dom)}]{dom}d[/{_dom_style(dom)}]",
        )
    console.print(table)


def _print_brand_sellthrough(full: bool = False) -> None:
    df = _load_brand_sellthrough_df()
    if df.is_empty():
        console.print("[dim]Not enough brand data yet (need ≥5 listings per brand).[/dim]")
        return

    df = df.with_columns(
        ((pl.col("sold_count") * 100.0) / pl.col("total")).round(1).alias("sellthrough_pct")
    )
    total = len(df)
    display = df if full else df.head(10)
    note = f"{total} brands" + (f" · showing top {len(display)}" if not full and total > 10 else "")

    table = Table(
        title=f"Brand Sell-Through  [dim]({note})[/dim]",
        box=box.ROUNDED, show_lines=True,
        header_style="bold white on dark_blue", title_style="bold cyan",
    )
    table.add_column("Brand",          style="cyan bold", min_width=14)
    table.add_column("Total",          justify="right",   style="white")
    table.add_column("Sold/Removed",   justify="right",   style="green")
    table.add_column("Sell-Through",   justify="right")
    table.add_column("Avg DOM (sold)", justify="right",   style="yellow")

    for row in display.iter_rows(named=True):
        pct = row["sellthrough_pct"]
        avg_dom = row["avg_dom_sold"]
        table.add_row(
            row["brand"],
            str(row["total"]),
            str(row["sold_count"]),
            f"[{_st_style(pct)}]{pct:.1f}%[/{_st_style(pct)}]",
            f"{avg_dom:.1f}d" if avg_dom is not None else "—",
        )
    console.print(table)


# strftime('%w'): 0=Sun, 1=Mon, …, 6=Sat
_DAY_NAMES = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday", 6: "Saturday"}
# Sort Mon(1)…Sat(6),Sun(0) → 0…6
_DAY_SORT = {d: (d + 6) % 7 for d in range(7)}


def _print_best_day() -> None:
    df = _load_day_df()
    if df.is_empty():
        console.print("[dim]No listing date data available yet.[/dim]")
        return

    df = df.with_columns([
        pl.col("day_num").replace_strict(_DAY_NAMES).alias("day_name"),
        pl.col("day_num").replace_strict(_DAY_SORT).alias("sort_key"),
        ((pl.col("sold_count") * 100.0) / pl.col("total")).round(1).alias("sellthrough_pct"),
    ]).sort("sort_key")

    table = Table(
        title="Best Day to List",
        box=box.ROUNDED, show_lines=True,
        header_style="bold white on dark_blue", title_style="bold cyan",
    )
    table.add_column("Day",            style="white",   min_width=11)
    table.add_column("Listed",         justify="right", style="dim")
    table.add_column("Sold/Removed",   justify="right", style="green")
    table.add_column("Sell-Through",   justify="right")
    table.add_column("Avg DOM (sold)", justify="right", style="yellow")

    for row in df.iter_rows(named=True):
        pct = row["sellthrough_pct"]
        avg_dom = row["avg_dom_sold"]
        table.add_row(
            row["day_name"],
            str(row["total"]),
            str(row["sold_count"]),
            f"[{_st_style(pct)}]{pct:.1f}%[/{_st_style(pct)}]",
            f"{avg_dom:.1f}d" if avg_dom is not None else "—",
        )
    console.print(table)
