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
    Compute per-(brand, category, size) metrics over sold items in the
    rolling N-day window.

    Note: Tradera's `?itemStatus=Ended` filter returns sold items only, so
    sell-through % is not meaningful (always 100% of the captured set).
    Velocity score = n * median_price / 1000 (kSEK of sold value), which
    weights volume and realized price together.
    """
    rows = conn.execute(
        """
        SELECT brand, category, size, final_price_sek
        FROM items
        WHERE brand IS NOT NULL
          AND category IS NOT NULL
          AND ended_at >= date('now', ? || ' days')
        """,
        (f"-{lookback_days}",),
    ).fetchall()

    groups: dict[tuple, list] = defaultdict(list)
    for row in rows:
        key = (row["brand"], row["category"], row["size"] or "Unknown")
        groups[key].append(dict(row))

    cells = []
    for (brand, category, size), items in groups.items():
        n = len(items)
        prices = [i["final_price_sek"] for i in items if i["final_price_sek"] is not None]
        median_price = round(_median(prices)) if prices else 0

        velocity_score = round(n * median_price / 1000, 1)

        cells.append({
            "brand": brand,
            "category": category,
            "size": size,
            "n": n,
            "n_priced": len(prices),
            "median_price_sek": median_price if prices else None,
            "p25_sek": round(_percentile(prices, 0.25)) if prices else None,
            "p75_sek": round(_percentile(prices, 0.75)) if prices else None,
            "total_value_kkr": velocity_score,
            "velocity_score": velocity_score,
            "low_confidence": n < 5,
        })

    cells.sort(key=lambda c: c["velocity_score"], reverse=True)
    return cells


def _velocity_class(score: float) -> str:
    if score >= 10.0:
        return "high"
    if score >= 3.0:
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
