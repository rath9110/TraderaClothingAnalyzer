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

    # Keep entries at all sample sizes — predict_price decides whether to
    # surface them as high-confidence (n ≥ MIN_N) or low-confidence fallbacks.
    out: dict[str, dict[str, dict]] = {code: {} for code, _ in LOOKUP_LEVELS}
    for code, group in buckets.items():
        for key, prices in group.items():
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
    Walk the fallback ladder from most specific to least specific and return
    the first level that has any data (n ≥ 1).  Specificity is always preferred
    over sample-size confidence: if the exact (brand×cat×size×channel) bucket
    has 3 items, return those 3 items marked 'low' rather than a broader bucket
    with n ≥ 10.  Returns None only when no data exists at any granularity.
    """
    inputs = {
        "brand": brand,
        "category": category,
        "size": size or "Unknown",
        "channel": channel,
        "condition": condition,
    }

    for code, fields in LOOKUP_LEVELS:
        if "condition" in fields and not condition:
            continue
        key = "|".join(inputs[f] for f in fields)
        entry = lookups[code].get(key)
        if entry and entry["n"] >= 1:
            confidence = "high" if entry["n"] >= MIN_N else "low"
            return {
                **entry,
                "granularity": code,
                "granularity_label": LEVEL_LABELS[code],
                "confidence": confidence,
            }
    return None


MIN_BRAND_TOTAL = 3  # drop brands with fewer than this many items across both channels


def build_cascade_index(conn: sqlite3.Connection, lookback_days: int = LOOKBACK_DAYS) -> dict:
    """
    Pre-compute counts for cascading dropdowns: pick brand → narrow category
    options → narrow size options → narrow condition options.  Each option
    carries (n_tradera, n_vinted) so the UI can grey out low-data choices.

    Brands with fewer than MIN_BRAND_TOTAL items across both channels are
    filtered out — they're useless for pricing and bloat the embedded JSON.
    """
    from collections import defaultdict

    rows = conn.execute(
        """
        SELECT brand, category, size, channel, condition, COUNT(*) AS n
        FROM items
        WHERE brand IS NOT NULL AND category IS NOT NULL
          AND (
            (channel = 'tradera' AND ended_at >= date('now', ? || ' days'))
         OR (channel = 'vinted'  AND last_seen_at >= date('now', ? || ' days'))
          )
        GROUP BY brand, category, size, channel, condition
        """,
        (f"-{lookback_days}", f"-{lookback_days}"),
    ).fetchall()

    nest = lambda: defaultdict(lambda: defaultdict(int))
    brand_n        = defaultdict(lambda: defaultdict(int))
    cat_by_b       = defaultdict(nest)
    size_by_bc     = defaultdict(nest)
    cond_by_bcs    = defaultdict(nest)

    for r in rows:
        b = r["brand"]; c = r["category"]; s = r["size"] or "Unknown"
        ch = r["channel"]; cond = r["condition"]; n = r["n"]

        brand_n[b][ch]                       += n
        cat_by_b[b][c][ch]                   += n
        size_by_bc[f"{b}|{c}"][s][ch]        += n
        if cond:
            cond_by_bcs[f"{b}|{c}|{s}"][cond][ch] += n

    def options_list(by_ch_dict):
        out = []
        for value, by_ch in by_ch_dict.items():
            n_t = by_ch.get("tradera", 0)
            n_v = by_ch.get("vinted", 0)
            out.append({"value": value, "n_tradera": n_t, "n_vinted": n_v, "total": n_t + n_v})
        out.sort(key=lambda x: -x["total"])
        return out

    # Filter brands below MIN_BRAND_TOTAL — singletons can't produce predictions
    # and they bloat the embedded JSON enormously (1098 of 1530 are singletons).
    brand_options = [b for b in options_list(brand_n) if b["total"] >= MIN_BRAND_TOTAL]
    kept_brands = {b["value"] for b in brand_options}

    return {
        "brands": brand_options,
        "cats_by_brand":      {b: options_list(d) for b, d in cat_by_b.items() if b in kept_brands},
        "sizes_by_bc":        {k: options_list(d) for k, d in size_by_bc.items() if k.split("|", 1)[0] in kept_brands},
        "conds_by_bcs":       {k: options_list(d) for k, d in cond_by_bcs.items() if k.split("|", 1)[0] in kept_brands},
        "min_n": MIN_N,
    }


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
