"""
Quick verification script: hit each configured Tradera category URL and confirm
it returns listings. Useful after adding new category IDs to categories.yaml.

Usage: python scripts/verify_categories.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
import yaml
from rich.console import Console
from rich.table import Table

CATEGORIES_FILE = Path("config/categories.yaml")
BASE_URL = "https://www.tradera.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TraderaVelocityAnalyzer/1.0; personal-research-tool)",
    "Accept-Language": "sv-SE,sv;q=0.9",
}

console = Console()


def main() -> None:
    with open(CATEGORIES_FILE, encoding="utf-8") as f:
        cats = yaml.safe_load(f)["categories"]

    table = Table(title="Category URL Verification")
    table.add_column("Label")
    table.add_column("ID")
    table.add_column("URL")
    table.add_column("HTTP Status")
    table.add_column("Has listings?")
    table.add_column("Verify?")

    with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        for cat in cats:
            url = f"{BASE_URL}/en/category/{cat['tradera_id']}"
            try:
                resp = client.get(url)
                status = resp.status_code
                has_listings = 'item-card-' in resp.text
                status_str = f"[green]{status}[/green]" if status == 200 else f"[red]{status}[/red]"
                listings_str = "[green]Yes[/green]" if has_listings else "[red]No[/red]"
            except Exception as e:
                status_str = f"[red]ERROR: {e}[/red]"
                listings_str = "–"

            table.add_row(
                cat["label"],
                str(cat["tradera_id"]),
                url,
                status_str,
                listings_str,
                "[yellow]⚠ Unverified[/yellow]" if cat.get("verify") else "[green]OK[/green]",
            )
            time.sleep(2)

    console.print(table)


if __name__ == "__main__":
    main()
