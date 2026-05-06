"""Generate the weekly HTML velocity report from the SQLite database."""
import math
import statistics
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

import jinja2

TEMPLATE_DIR = Path(__file__).parent / "templates"
REPORTS_DIR = Path("reports")


def _median(values: list) -> float:
    return statistics.median(values) if values else 0.0


def _percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(len(s) * pct)
    return float(s[min(idx, len(s) - 1)])


def compute_metrics(conn: sqlite3.Connection, lookback_days: int = 90) -> list[dict]:
    """
    Query items from the last N days and compute per-(brand, category, size) metrics.
    Returns list of cell dicts sorted by velocity_score descending.
    """
    rows = conn.execute(
        """
        SELECT brand, category, size, final_price_sek, bid_count, had_bids
        FROM items
        WHERE brand IS NOT NULL
          AND category IS NOT NULL
          AND ended_at >= date('now', ? || ' days')
        """,
        (f"-{lookback_days}",),
    ).fetchall()

    # Group by (brand, category, size)
    groups: dict[tuple, list] = defaultdict(list)
    for row in rows:
        key = (row["brand"], row["category"], row["size"] or "Unknown")
        groups[key].append(dict(row))

    cells = []
    for (brand, category, size), items in groups.items():
        n = len(items)
        prices = [i["final_price_sek"] for i in items if i["final_price_sek"] is not None]
        sold_items = [i for i in items if i["had_bids"]]
        sell_through = len(sold_items) / n if n else 0.0
        bid_counts = [i["bid_count"] for i in items if i["bid_count"] is not None]

        velocity_score = round(sell_through * math.log(n + 1), 3)

        cells.append({
            "brand": brand,
            "category": category,
            "size": size,
            "n": n,
            "sell_through_pct": round(sell_through * 100, 1),
            "median_price_sek": round(_median(prices)) if prices else None,
            "p25_sek": round(_percentile(prices, 0.25)) if prices else None,
            "p75_sek": round(_percentile(prices, 0.75)) if prices else None,
            "median_bids": round(_median(bid_counts), 1) if bid_counts else 0.0,
            "velocity_score": velocity_score,
            "low_confidence": n < 5,
        })

    cells.sort(key=lambda c: c["velocity_score"], reverse=True)
    return cells


def _velocity_class(score: float) -> str:
    if score >= 1.5:
        return "high"
    if score >= 0.8:
        return "med"
    return "low"


def generate_report(conn: sqlite3.Connection, output_path: Optional[Path] = None) -> Path:
    cells = compute_metrics(conn)
    top_picks = [c for c in cells if not c["low_confidence"]][:20]

    total_items = conn.execute(
        "SELECT COUNT(*) FROM items WHERE ended_at >= date('now', '-90 days')"
    ).fetchone()[0]
    brand_count = conn.execute(
        "SELECT COUNT(DISTINCT brand) FROM items WHERE brand IS NOT NULL AND ended_at >= date('now', '-90 days')"
    ).fetchone()[0]

    # Add velocity_class for template coloring
    for cell in cells:
        cell["velocity_class"] = _velocity_class(cell["velocity_score"])

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
    )
    template = env.get_template("report.html.j2")

    html = template.render(
        generated_at=date.today().isoformat(),
        week=date.today().isocalendar()[1],
        total_items=total_items,
        brand_count=brand_count,
        top_picks=top_picks,
        all_cells=cells,
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if output_path is None:
        output_path = REPORTS_DIR / f"{date.today().isoformat()}.html"

    output_path.write_text(html, encoding="utf-8")
    return output_path
