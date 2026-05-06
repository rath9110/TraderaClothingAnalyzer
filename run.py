"""
Weekly batch runner for Tradera Velocity Intelligence.

Usage:
    python run.py            # scrape all categories + generate report
    python run.py --report   # report only (no new scraping)
    python run.py --audit    # run selector-drift audit on cached HTML
    python run.py --cat dam_tröjor_stickade  # scrape one category only
"""
import argparse
import logging
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from tradera.db import get_connection, setup_db, upsert_items_batch, log_run_start, log_run_complete
from tradera.parser import BrandMatcher, normalize_item
from tradera.report import generate_report
from tradera.scraper import TraderaScraper

console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)

CATEGORIES_FILE = Path("config/categories.yaml")
MAX_PAGES_PER_CATEGORY = 30  # ~1800 items; covers ~90 days for most brand-level searches


def load_categories(path: Path = CATEGORIES_FILE) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["categories"]


def run_scrape(category_filter: str | None = None) -> int:
    categories = load_categories()
    if category_filter:
        categories = [c for c in categories if c["label"] == category_filter]
        if not categories:
            console.print(f"[red]Unknown category label: {category_filter}[/red]")
            sys.exit(1)

    # Warn about unverified men's category IDs
    unverified = [c for c in categories if c.get("verify")]
    if unverified:
        console.print(
            f"[yellow]Note: {len(unverified)} men's category IDs are unverified — "
            "check config/categories.yaml and confirm by visiting each Tradera URL.[/yellow]"
        )

    brand_matcher = BrandMatcher()
    conn = get_connection()
    setup_db(conn)

    labels = [c["label"] for c in categories]
    run_id = log_run_start(conn, labels)

    total_upserted = 0

    with TraderaScraper() as scraper:
        for cat in categories:
            label = cat["label"]
            cat_id = cat["tradera_id"]
            console.print(f"\n[bold cyan]Scraping:[/bold cyan] {cat['display']} (id={cat_id})")

            raw_items = scraper.scrape_category(
                category_id=cat_id,
                category_label=label,
                max_pages=MAX_PAGES_PER_CATEGORY,
            )

            if not raw_items:
                console.print(f"  [yellow]⚠ No items returned — possible scrape failure or empty category.[/yellow]")
                continue

            normalized = [normalize_item(r, brand_matcher) for r in raw_items]
            matched = [i for i in normalized if i["brand"] is not None]

            count = upsert_items_batch(conn, normalized)
            total_upserted += count

            console.print(
                f"  {len(raw_items)} raw → {count} upserted "
                f"({len(matched)} brand-matched, "
                f"{len(raw_items) - len(matched)} unmatched)"
            )

    log_run_complete(conn, run_id, total_upserted)
    conn.close()
    console.print(f"\n[green]Done.[/green] Total items upserted: {total_upserted}")
    return total_upserted


def run_report() -> Path:
    conn = get_connection()
    setup_db(conn)
    path = generate_report(conn)
    conn.close()
    console.print(f"[green]Report written:[/green] {path}")
    return path


def run_audit() -> None:
    """
    Selector-drift audit: re-parse cached HTML and report null rates.
    High null rates on title/price suggest Tradera changed their HTML structure.
    """
    from tradera.scraper import TraderaScraper, CACHE_DIR
    import gzip
    from selectolax.parser import HTMLParser

    cache_dir = Path(CACHE_DIR)
    files = sorted(cache_dir.glob("*.html.gz"))

    if not files:
        console.print("[yellow]No cached HTML files found. Run a scrape first.[/yellow]")
        return

    # Sample up to 20 recent files
    sample = files[-20:]
    title_nulls = price_nulls = bids_nulls = total = 0

    for f in sample:
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            html = fh.read()
        tree = HTMLParser(html)
        cards = tree.css('[id^="item-card-"]')
        for card in cards:
            total += 1
            title_node = (
                card.css_first('[data-testid="title"]')
                or card.css_first('[class*="__title"]')
                or card.css_first('[class*="title"]')
            )
            price_node = card.css_first('[data-testid="price"]')
            bids_node = card.css_first('[data-testid="bids-label"]')
            if not title_node or not title_node.text(strip=True):
                title_nulls += 1
            if not price_node:
                price_nulls += 1
            if not bids_node:
                bids_nulls += 1

    if total == 0:
        console.print("[yellow]No item cards found in cached HTML. Possible selector drift.[/yellow]")
        return

    table = Table(title=f"Selector Audit — {total} items from {len(sample)} cached pages")
    table.add_column("Field")
    table.add_column("Null count")
    table.add_column("Null %")
    table.add_column("Status")

    def row(name: str, nulls: int) -> None:
        pct = nulls / total * 100
        status = "[green]OK[/green]" if pct < 20 else "[red]DRIFT DETECTED[/red]"
        table.add_row(name, str(nulls), f"{pct:.1f}%", status)

    row("title", title_nulls)
    row("price", price_nulls)
    row("bids", bids_nulls)

    console.print(table)
    if max(title_nulls, price_nulls) / total > 0.2:
        console.print(
            "[red bold]⚠ Selector drift likely — update selectors in tradera/scraper.py "
            "before running next full scrape.[/red bold]"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Tradera Velocity Intelligence — weekly batch tool")
    parser.add_argument("--report", action="store_true", help="Generate report only (no scraping)")
    parser.add_argument("--audit", action="store_true", help="Run selector-drift audit on cached HTML")
    parser.add_argument("--cat", metavar="LABEL", help="Scrape a single category by label")
    args = parser.parse_args()

    if args.audit:
        run_audit()
    elif args.report:
        run_report()
    else:
        run_scrape(category_filter=args.cat)
        run_report()


if __name__ == "__main__":
    main()
