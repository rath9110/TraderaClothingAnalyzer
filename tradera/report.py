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


def _price_for_channel(row: dict) -> Optional[int]:
    """Tradera = realized final price; Vinted = listed price (proxy)."""
    if row["channel"] == "tradera":
        return row["final_price_sek"]
    return row["listed_price_sek"] or row["final_price_sek"]


def _velocity_class(score: float) -> str:
    if score >= 10.0:
        return "high"
    if score >= 3.0:
        return "med"
    return "low"


def compute_metrics(conn: sqlite3.Connection, lookback_days: int = 90) -> list[dict]:
    """
    Per-(brand, category, size, channel) cell metrics over the rolling window.
    Tradera uses final_price_sek (realized); Vinted uses listed_price_sek (asking).
    """
    rows = conn.execute(
        """
        SELECT brand, category, size, channel, final_price_sek, listed_price_sek,
               time_to_sell_days
        FROM items
        WHERE brand IS NOT NULL AND category IS NOT NULL
          AND (
              (channel = 'tradera' AND ended_at >= date('now', ? || ' days'))
           OR (channel = 'vinted'  AND last_seen_at >= date('now', ? || ' days'))
          )
        """,
        (f"-{lookback_days}", f"-{lookback_days}"),
    ).fetchall()

    groups: dict[tuple, list] = defaultdict(list)
    for row in rows:
        key = (row["brand"], row["category"], row["size"] or "Unknown", row["channel"])
        groups[key].append(dict(row))

    cells = []
    for (brand, category, size, channel), items in groups.items():
        n = len(items)
        prices = [p for p in (_price_for_channel(i) for i in items) if p is not None]
        median_price = round(_median(prices)) if prices else 0
        ttsd = [i["time_to_sell_days"] for i in items if i["time_to_sell_days"] is not None]

        cells.append({
            "brand": brand,
            "category": category,
            "size": size,
            "channel": channel,
            "n": n,
            "median_price_sek": median_price if prices else None,
            "p25_sek": round(_percentile(prices, 0.25)) if prices else None,
            "p75_sek": round(_percentile(prices, 0.75)) if prices else None,
            "median_time_to_sell": round(_median(ttsd), 1) if ttsd else None,
            "n_with_cycle_time": len(ttsd),
            "velocity_score": round(n * median_price / 1000, 1),
            "low_confidence": n < 5,
        })

    cells.sort(key=lambda c: c["velocity_score"], reverse=True)
    return cells


def compute_cross_channel(cells: list[dict]) -> list[dict]:
    """
    For each (brand, category) where BOTH channels have data, build a
    side-by-side row showing Tradera realized vs Vinted asking prices and
    the price delta the user can arbitrage.
    """
    by_bc: dict[tuple, dict] = defaultdict(dict)
    for cell in cells:
        if cell["low_confidence"]:
            continue
        # Aggregate sizes — channel-level, brand × category granularity
        key = (cell["brand"], cell["category"])
        bucket = by_bc[key].setdefault(cell["channel"], {"n": 0, "weighted_sum": 0, "median_prices": []})
        bucket["n"] += cell["n"]
        if cell["median_price_sek"]:
            bucket["weighted_sum"] += cell["median_price_sek"] * cell["n"]
            bucket["median_prices"].append((cell["median_price_sek"], cell["n"]))

    rows = []
    for (brand, category), channels in by_bc.items():
        if not ({"tradera", "vinted"} <= channels.keys()):
            continue  # need both channels

        t = channels["tradera"]; v = channels["vinted"]
        if not t["weighted_sum"] or not v["weighted_sum"]:
            continue

        t_avg = round(t["weighted_sum"] / t["n"])
        v_avg = round(v["weighted_sum"] / v["n"])
        delta_abs = t_avg - v_avg
        delta_pct = round(delta_abs / v_avg * 100, 1) if v_avg else 0

        rows.append({
            "brand": brand,
            "category": category,
            "tradera_n": t["n"],
            "tradera_avg": t_avg,
            "vinted_n": v["n"],
            "vinted_avg": v_avg,
            "delta_sek": delta_abs,
            "delta_pct": delta_pct,
            "premium_channel": "tradera" if delta_abs > 0 else "vinted",
        })

    rows.sort(key=lambda r: abs(r["delta_pct"]), reverse=True)
    return rows


def compute_cycle_time_insights(conn: sqlite3.Connection, lookback_days: int = 90) -> list[dict]:
    """
    Rows where we have an inferred time-to-sell (Vinted, longitudinal).
    Empty until items disappear between runs (~2+ weekly runs needed).
    """
    rows = conn.execute(
        """
        SELECT brand, category, size,
               COUNT(*) AS n_sold,
               ROUND(AVG(time_to_sell_days), 1) AS avg_days,
               ROUND(AVG(listed_price_sek)) AS avg_price
        FROM items
        WHERE channel = 'vinted'
          AND time_to_sell_days IS NOT NULL
          AND brand IS NOT NULL
          AND last_seen_at >= date('now', ? || ' days')
        GROUP BY brand, category, size
        HAVING n_sold >= 3
        ORDER BY avg_days ASC
        """,
        (f"-{lookback_days}",),
    ).fetchall()
    return [dict(r) for r in rows]


def generate_report(conn: sqlite3.Connection, output_path: Optional[Path] = None) -> Path:
    cells = compute_metrics(conn)
    cross_channel = compute_cross_channel(cells)
    cycle_time = compute_cycle_time_insights(conn)

    top_picks = [c for c in cells if not c["low_confidence"]][:20]

    total_t = conn.execute(
        "SELECT COUNT(*) FROM items WHERE channel = 'tradera' AND ended_at >= date('now','-90 days')"
    ).fetchone()[0]
    total_v = conn.execute(
        "SELECT COUNT(*) FROM items WHERE channel = 'vinted' AND last_seen_at >= date('now','-90 days')"
    ).fetchone()[0]
    brand_count = conn.execute(
        "SELECT COUNT(DISTINCT brand) FROM items WHERE brand IS NOT NULL"
    ).fetchone()[0]

    for c in cells:
        c["velocity_class"] = _velocity_class(c["velocity_score"])

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
    )
    template = env.get_template("report.html.j2")

    html = template.render(
        generated_at=date.today().isoformat(),
        week=date.today().isocalendar()[1],
        total_tradera=total_t,
        total_vinted=total_v,
        brand_count=brand_count,
        top_picks=top_picks,
        all_cells=cells,
        cross_channel=cross_channel,
        cycle_time=cycle_time,
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if output_path is None:
        output_path = REPORTS_DIR / f"{date.today().isoformat()}.html"
    output_path.write_text(html, encoding="utf-8")
    return output_path
