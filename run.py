"""
Weekly batch runner for Tradera + Vinted Velocity Intelligence.

Usage:
    python run.py                       # both channels + report
    python run.py --channel tradera     # tradera only
    python run.py --channel vinted      # vinted only
    python run.py --report              # report only (no scrape)
    python run.py --audit               # selector-drift audit on Tradera cache
    python run.py --cat dam_tröjor_stickade   # one category only
    python run.py --no-cache            # bypass cached HTML
"""
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from tradera.db import (
    get_connection, setup_db, upsert_items_batch,
    log_run_start, log_run_complete, mark_disappeared_items_sold,
)
from tradera.parser import BrandMatcher, normalize_item
from tradera.report import generate_report
from tradera.scraper import TraderaScraper
from tradera.vinted import VintedScraper, normalize_vinted_item

console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)

CATEGORIES_FILE = Path("config/categories.yaml")
TRADERA_MAX_PAGES = 30
VINTED_MAX_PAGES = 10


def load_categories(path: Path = CATEGORIES_FILE) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["categories"]


def run_tradera_scrape(category_filter: str | None, no_cache: bool, brand_matcher: BrandMatcher) -> int:
    categories = load_categories()
    if category_filter:
        categories = [c for c in categories if c["label"] == category_filter]
        if not categories:
            console.print(f"[red]Unknown category label: {category_filter}[/red]")
            return 0

    conn = get_connection()
    setup_db(conn)
    labels = [c["label"] for c in categories]
    run_id = log_run_start(conn, labels, channel="tradera")
    total = 0

    with TraderaScraper(use_cache=not no_cache) as scraper:
        for cat in categories:
            console.print(f"\n[bold cyan]Tradera:[/bold cyan] {cat['display']} (id={cat['tradera_id']})")
            raw = scraper.scrape_category(cat["tradera_id"], cat["label"], TRADERA_MAX_PAGES)
            if not raw:
                console.print("  [yellow]⚠ No items returned[/yellow]")
                continue
            items = [normalize_item(r, brand_matcher) for r in raw]
            matched = sum(1 for i in items if i["brand"])
            n = upsert_items_batch(conn, items)
            total += n
            console.print(f"  {len(raw)} raw → {n} upserted ({matched} brand-matched)")

    log_run_complete(conn, run_id, total)
    conn.close()
    return total


def run_vinted_scrape(category_filter: str | None, no_cache: bool, brand_matcher: BrandMatcher) -> int:
    """
    Scrape ACTIVE Vinted listings.  Items present in this run get last_seen_at
    refreshed; items absent from this run (but previously seen) get marked
    sold and assigned time_to_sell_days = last_seen − first_seen.
    """
    all_cats = load_categories()
    cats = [c for c in all_cats if c.get("vinted_id") and c.get("vinted_slug")]
    if category_filter:
        cats = [c for c in cats if c["label"] == category_filter]
        if not cats:
            console.print(f"[yellow]No Vinted-mapped category for label: {category_filter}[/yellow]")
            return 0

    if not cats:
        console.print("[yellow]No categories have vinted_id + vinted_slug.[/yellow]")
        return 0

    conn = get_connection()
    setup_db(conn)
    run_started = datetime.utcnow().isoformat()
    labels = [c["label"] for c in cats]
    run_id = log_run_start(conn, labels, channel="vinted")
    total = 0

    with VintedScraper(use_cache=not no_cache) as scraper:
        for cat in cats:
            console.print(f"\n[bold magenta]Vinted:[/bold magenta] {cat['display']} (id={cat['vinted_id']}-{cat['vinted_slug']})")
            raw = scraper.scrape_category(
                cat["vinted_id"], cat["label"], cat["vinted_slug"], VINTED_MAX_PAGES,
            )
            if not raw:
                console.print("  [yellow]⚠ No items returned[/yellow]")
                continue
            items = [normalize_vinted_item(r, brand_matcher) for r in raw]
            matched = sum(1 for i in items if i["brand"])
            n = upsert_items_batch(conn, items)
            total += n
            console.print(f"  {len(raw)} raw → {n} upserted ({matched} brand-matched)")

    # Items in DB but not seen this run → infer sold + time-to-sell
    sold_marked = mark_disappeared_items_sold(conn, "vinted", run_started)
    if sold_marked:
        console.print(f"\n[green]Inferred {sold_marked} Vinted items as sold (disappeared since last run)[/green]")

    log_run_complete(conn, run_id, total)
    conn.close()
    return total


def run_report() -> Path:
    conn = get_connection()
    setup_db(conn)
    path = generate_report(conn)
    conn.close()
    console.print(f"[green]Report:[/green] {path}")
    return path


def run_price(brand: str, category: str, size: str, channel: str, condition: str | None = None) -> None:
    """CLI: estimate a price using the hierarchical lookup."""
    from tradera.pricing import build_lookups, predict_price

    conn = get_connection()
    setup_db(conn)
    lookups = build_lookups(conn)
    conn.close()

    if size.lower() in ("none", "null", "-"):
        size = None
    result = predict_price(brand, category, size, channel, lookups, condition=condition)

    if not result:
        cond_str = f" / {condition}" if condition else ""
        console.print(
            f"[yellow]No data for {brand} / {category} / {size or 'Unknown'}{cond_str} / {channel} "
            "at any granularity (need n ≥ 10).[/yellow]"
        )
        return

    console.print(f"\n[bold green]{result['median']} kr[/bold green]"
                  f"  [dim](range {result['p25']} – {result['p75']} kr, n={result['n']})[/dim]")
    console.print(f"[dim]Matched on:[/dim] {result['granularity_label']}")


def run_backfill_conditions() -> None:
    """Re-parse cached Vinted HTML and populate condition for existing rows."""
    from tradera.vinted import backfill_conditions_from_cache

    conn = get_connection()
    setup_db(conn)
    n_updated = backfill_conditions_from_cache(conn)
    total_with_cond = conn.execute(
        "SELECT COUNT(*) FROM items WHERE condition IS NOT NULL"
    ).fetchone()[0]
    total_vinted = conn.execute(
        "SELECT COUNT(*) FROM items WHERE channel = 'vinted'"
    ).fetchone()[0]
    conn.close()
    console.print(
        f"[green]Backfilled condition on {n_updated} rows. "
        f"Now {total_with_cond}/{total_vinted} Vinted items have a condition.[/green]"
    )


def run_audit() -> None:
    from tradera.scraper import CACHE_DIR
    import gzip
    from selectolax.parser import HTMLParser

    cache_dir = Path(CACHE_DIR)
    files = sorted(cache_dir.glob("*.html.gz"))
    if not files:
        console.print("[yellow]No cached Tradera HTML. Run a scrape first.[/yellow]")
        return

    sample = files[-20:]
    title_nulls = price_nulls = total = 0
    for f in sample:
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            html = fh.read()
        tree = HTMLParser(html)
        cards = [c for c in tree.css('[id^="item-card-"]') if c.attributes.get("data-item-type")]
        for card in cards:
            total += 1
            link = card.css_first('a[href*="/item/"]')
            if not link or not (link.attributes.get("aria-label") or link.attributes.get("title")):
                title_nulls += 1
            if not card.css_first('[data-testid="price"]'):
                price_nulls += 1

    if total == 0:
        console.print("[red]No item cards found — possible selector drift.[/red]")
        return

    table = Table(title=f"Tradera selector audit — {total} items / {len(sample)} pages")
    table.add_column("Field"); table.add_column("Null %"); table.add_column("Status")
    for name, nulls in [("title", title_nulls), ("price", price_nulls)]:
        pct = nulls / total * 100
        status = "[green]OK[/green]" if pct < 20 else "[red]DRIFT[/red]"
        table.add_row(name, f"{pct:.1f}%", status)
    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", choices=["tradera", "vinted", "both"], default="both")
    parser.add_argument("--report", action="store_true", help="Report only")
    parser.add_argument("--audit", action="store_true", help="Selector-drift audit")
    parser.add_argument("--cat", metavar="LABEL", help="Single category by label")
    parser.add_argument("--no-cache", action="store_true", help="Bypass cached HTML")
    parser.add_argument(
        "--price", nargs="+", metavar=("BRAND", "CATEGORY"),
        help='Predict price: --price BRAND CATEGORY SIZE CHANNEL [CONDITION]',
    )
    parser.add_argument(
        "--backfill-conditions", action="store_true",
        help="Re-parse cached Vinted HTML to populate the new condition column",
    )
    args = parser.parse_args()

    if args.backfill_conditions:
        run_backfill_conditions()
        return
    if args.price:
        if len(args.price) not in (4, 5):
            console.print("[red]--price expects 4 or 5 args: BRAND CATEGORY SIZE CHANNEL [CONDITION][/red]")
            return
        run_price(*args.price)
        return
    if args.audit:
        run_audit()
        return
    if args.report:
        run_report()
        return

    brand_matcher = BrandMatcher()

    if args.channel in ("tradera", "both"):
        run_tradera_scrape(args.cat, args.no_cache, brand_matcher)
    if args.channel in ("vinted", "both"):
        run_vinted_scrape(args.cat, args.no_cache, brand_matcher)

    run_report()


if __name__ == "__main__":
    main()
