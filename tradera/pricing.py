"""
Hierarchical median pricing — Phase 1.

For a given (brand, category, size, channel), descend through fall-back
levels until one has at least MIN_N observations.  Returns the median plus
p25/p75 of realised prices (Tradera) or listed prices (Vinted).

Why median, not mean: secondhand prices are right-skewed (collectible
outliers).  Median + IQR gives a defensible price band the user can pick
within based on how fast they want to clear.
"""
import sqlite3
import statistics
from collections import defaultdict
from typing import Optional

# Most specific → least specific.
# Each level: (code, key fields).  All include `channel` since Tradera
# realised prices and Vinted asking prices live on different scales.
# `Z` suffix marks condition-aware levels.
LOOKUP_LEVELS = [
    # Condition-aware (used only when caller supplies condition)
    ("bcscZ", ["brand", "category", "size", "channel", "condition"]),
    ("bccZ",  ["brand", "category", "channel", "condition"]),
    ("bchZ",  ["brand", "channel", "condition"]),
    ("cscZ",  ["category", "size", "channel", "condition"]),
    ("ccZ",   ["category", "channel", "condition"]),
    # Condition-agnostic fallbacks (always tried)
    ("bcsc",  ["brand", "category", "size", "channel"]),
    ("bcc",   ["brand", "category", "channel"]),
    ("bch",   ["brand", "channel"]),
    ("csc",   ["category", "size", "channel"]),
    ("cc",    ["category", "channel"]),
]

LEVEL_LABELS = {
    "bcscZ": "brand × category × size × channel × condition",
    "bccZ":  "brand × category × channel × condition",
    "bchZ":  "brand × channel × condition",
    "cscZ":  "category × size × channel × condition",
    "ccZ":   "category × channel × condition",
    "bcsc":  "brand × category × size × channel",
    "bcc":   "brand × category × channel",
    "bch":   "brand × channel",
    "csc":   "category × size × channel",
    "cc":    "category × channel",
}

MIN_N = 10  # minimum sample size for a level to qualify
LOOKBACK_DAYS = 90


def _price_for_row(row: dict) -> Optional[int]:
    """Tradera = realised final price; Vinted = listed asking price."""
    if row["channel"] == "tradera":
        return row["final_price_sek"]
    return row["listed_price_sek"]


def build_lookups(conn: sqlite3.Connection, lookback_days: int = LOOKBACK_DAYS) -> dict:
    """
    Pre-compute median + p25/p75 lookups at every granularity.
    Returns: {level_code: {key_string: {median, p25, p75, n}}}
    """
    rows = conn.execute(
        """
        SELECT brand, category, size, channel, condition,
               final_price_sek, listed_price_sek
        FROM items
        WHERE brand IS NOT NULL AND category IS NOT NULL
          AND (
            (channel = 'tradera' AND ended_at >= date('now', ? || ' days'))
         OR (channel = 'vinted'  AND last_seen_at >= date('now', ? || ' days'))
          )
        """,
        (f"-{lookback_days}", f"-{lookback_days}"),
    ).fetchall()

    buckets: dict[str, dict[str, list]] = {code: defaultdict(list) for code, _ in LOOKUP_LEVELS}

    for row in rows:
        d = dict(row)
        d["size"] = d["size"] or "Unknown"
        price = _price_for_row(d)
        if price is None:
            continue
        for code, fields in LOOKUP_LEVELS:
            # Skip condition-aware levels for rows lacking condition
            if "condition" in fields and not d.get("condition"):
                continue
            key = "|".join(str(d[f]) for f in fields)
            buckets[code][key].append(price)

    out: dict[str, dict[str, dict]] = {code: {} for code, _ in LOOKUP_LEVELS}
    for code, group in buckets.items():
        for key, prices in group.items():
            if len(prices) < MIN_N:
                continue
            s = sorted(prices)
            n = len(s)
            out[code][key] = {
                "median": round(statistics.median(s)),
                "p25": round(s[int(n * 0.25)]),
                "p75": round(s[int(n * 0.75)]),
                "n": n,
            }
    return out


def predict_price(
    brand: str,
    category: str,
    size: Optional[str],
    channel: str,
    lookups: dict,
    condition: Optional[str] = None,
) -> Optional[dict]:
    """
    Walk the fallback ladder; return first match with n >= MIN_N or None.
    Condition-aware levels are tried first when `condition` is provided,
    then condition-agnostic fallbacks.
    """
    inputs = {
        "brand": brand,
        "category": category,
        "size": size or "Unknown",
        "channel": channel,
        "condition": condition,
    }

    for code, fields in LOOKUP_LEVELS:
        # Skip condition-aware levels if no condition supplied
        if "condition" in fields and not condition:
            continue
        key = "|".join(inputs[f] for f in fields)
        entry = lookups[code].get(key)
        if entry:
            return {
                **entry,
                "granularity": code,
                "granularity_label": LEVEL_LABELS[code],
            }
    return None


def get_distinct_values(conn: sqlite3.Connection, lookback_days: int = LOOKBACK_DAYS) -> dict:
    """Distinct dropdown values for the report's calculator form."""
    base_where = """
        brand IS NOT NULL AND category IS NOT NULL
        AND (
          (channel = 'tradera' AND ended_at >= date('now', ? || ' days'))
       OR (channel = 'vinted'  AND last_seen_at >= date('now', ? || ' days'))
        )
    """
    days_param = f"-{lookback_days}"

    brands = [r[0] for r in conn.execute(
        f"SELECT DISTINCT brand FROM items WHERE {base_where} ORDER BY brand",
        (days_param, days_param),
    ).fetchall()]
    categories = [r[0] for r in conn.execute(
        f"SELECT DISTINCT category FROM items WHERE {base_where} ORDER BY category",
        (days_param, days_param),
    ).fetchall()]
    sizes = [r[0] for r in conn.execute(
        f"SELECT DISTINCT COALESCE(size, 'Unknown') FROM items WHERE {base_where} ORDER BY 1",
        (days_param, days_param),
    ).fetchall()]
    channels = [r[0] for r in conn.execute(
        f"SELECT DISTINCT channel FROM items WHERE {base_where} ORDER BY channel",
        (days_param, days_param),
    ).fetchall()]

    conditions = [r[0] for r in conn.execute(
        f"SELECT DISTINCT condition FROM items WHERE condition IS NOT NULL AND {base_where} ORDER BY condition",
        (days_param, days_param),
    ).fetchall()]

    return {
        "brands": brands,
        "categories": categories,
        "sizes": sizes,
        "channels": channels,
        "conditions": conditions,
    }
