"""
Parse raw item dicts from scraper into normalized DB-ready records.
Brand extraction via whitelist regex. Size via pattern matching.
"""
import re
import yaml
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

BRANDS_FILE = Path("config/brands.yaml")

SWEDISH_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
    "januari": 1, "februari": 2, "mars": 3, "april": 4, "juni": 6,
    "juli": 7, "augusti": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
}

# EU letter sizes — match as whole word
_LETTER_SIZES = r"\b(XXS|XS|S|M|L|XL|2XL|3XL|XXL|XXXL)\b"
# Swedish "stl" prefix
_STL_SIZES = r"\bstl\.?\s*([0-9]+[a-zA-Z]?|XXS|XS|S|M|L|XL|XXL)\b"
# EU numeric clothing sizes 32–58
_NUMERIC_SIZES = r"\b(3[2-9]|4[0-9]|5[0-8])\b"
# Jeans W/L  e.g. 28/32 or 28x32
_JEANS_SIZES = r"\b([2-9][0-9])[x/]([2-9][0-9])\b"

RE_LETTER = re.compile(_LETTER_SIZES, re.IGNORECASE)
RE_STL = re.compile(_STL_SIZES, re.IGNORECASE)
RE_NUMERIC = re.compile(_NUMERIC_SIZES)
RE_JEANS = re.compile(_JEANS_SIZES)
# Tradera prices appear as "220 SEK" (with U+00A0 non-breaking space) on /en/ pages
# and as "220 kr" on /sv/ pages.  Match either, with any whitespace separator.
RE_PRICE = re.compile(r"([\d][\d\s ]*)\s*(?:SEK|kr)\b", re.IGNORECASE)
RE_BIDS = re.compile(r"(\d+)\s*bud", re.IGNORECASE)


class BrandMatcher:
    def __init__(self, brands_file: Path = BRANDS_FILE):
        with open(brands_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # Build (pattern, brand_name, tier) tuples, longest alias first
        self._rules: list[tuple[re.Pattern, str, str]] = []
        seen_names: set[str] = set()

        for brand in data["brands"]:
            name = brand["name"]
            if name in seen_names or not brand.get("aliases"):
                seen_names.add(name)
                continue
            seen_names.add(name)
            tier = brand.get("tier", "mid")
            for alias in brand["aliases"]:
                if not alias:
                    continue
                try:
                    pattern = re.compile(alias, re.IGNORECASE)
                    self._rules.append((pattern, name, tier))
                except re.error as e:
                    log.warning("Bad regex for brand %s alias %r: %s", name, alias, e)

    def match(self, title: str) -> Optional[str]:
        """Return matched brand name or None."""
        for pattern, name, _tier in self._rules:
            if pattern.search(title):
                return name
        return None


def extract_size(title: str) -> Optional[str]:
    """Extract the most specific size string from a title."""
    # Jeans W/L first (most specific)
    m = RE_JEANS.search(title)
    if m:
        return f"{m.group(1)}/{m.group(2)}"

    # "stl XX" prefix
    m = RE_STL.search(title)
    if m:
        return m.group(1).upper()

    # EU letter sizes
    m = RE_LETTER.search(title)
    if m:
        return m.group(1).upper()

    # EU numeric (avoid matching years/prices)
    m = RE_NUMERIC.search(title)
    if m:
        return m.group(1)

    return None


def parse_price(price_text: str) -> Optional[int]:
    """'360 kr' → 360, '1 200 kr' → 1200, '' → None"""
    if not price_text:
        return None
    m = RE_PRICE.search(price_text)
    if not m:
        return None
    digits = re.sub(r"\s+", "", m.group(1))
    try:
        return int(digits)
    except ValueError:
        return None


def parse_bid_count(bids_text: str) -> tuple[Optional[int], int]:
    """Returns (bid_count, had_bids). '5 bud' → (5, 1), '' → (None, 0)"""
    if not bids_text:
        return None, 0
    m = RE_BIDS.search(bids_text)
    if m:
        count = int(m.group(1))
        return count, (1 if count > 0 else 0)
    # Text present but no match (e.g. "Inga bud" = no bids)
    return 0, 0


def parse_end_date(end_text: str) -> Optional[str]:
    """
    Parse end-date strings from Tradera listing cards.
    Returns ISO date string (YYYY-MM-DD) or None.

    On Tradera's `?itemStatus=Ended` listings, the time element shows just
    "Ended" with no exact date — return None and let the caller fall back to
    scraped_at.  Active-auction cards do show "10 maj 23:10" / "10 May 14:43"
    style end times, which we still support for completeness.
    """
    if not end_text:
        return None

    text = end_text.lower().strip()
    today = date.today()

    # No exact date available — caller substitutes scraped_at
    if text in {"ended", "avslutad", "avslutades"}:
        return None

    if text.startswith("idag"):
        return today.isoformat()

    if text.startswith("igår"):
        from datetime import timedelta
        return (today - timedelta(days=1)).isoformat()

    # English month names (Tradera /en/ pages use these for active auctions)
    en_months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    m = re.search(
        r"(\d{1,2})\s+([a-zåäö]+)(?:\s+(\d{4})|\s+\d{2}:\d{2})?",
        text,
    )
    if m:
        day = int(m.group(1))
        month_str = m.group(2)[:3]
        year = int(m.group(3)) if m.group(3) else None
        month = SWEDISH_MONTHS.get(month_str) or en_months.get(month_str)
        if not month:
            return None

        try:
            if not year:
                candidate = date(today.year, month, day)
                year = today.year - 1 if candidate > today else today.year
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    return None


def normalize_item(raw: dict, brand_matcher: BrandMatcher) -> dict:
    """
    Convert a raw scraper dict into a normalized DB-ready record.

    For Tradera's `?itemStatus=Ended` filter, all returned items are sold
    (had_bids = 1 by definition).  ended_at falls back to today's date when
    the listing card only shows "Ended" without a specific date.
    """
    title = raw.get("title") or raw.get("raw_title") or ""
    price_sek = parse_price(raw.get("price_text", ""))
    bid_count, _ = parse_bid_count(raw.get("bids_text", ""))

    ended_at = parse_end_date(raw.get("end_text", ""))
    if ended_at is None:
        # Listing card showed "Ended" with no date, or PureBin items with no
        # time node — fall back to today (the scrape date).
        ended_at = date.today().isoformat()

    # Items returned under ?itemStatus=Ended are sold by definition of the
    # filter; trust price presence as the signal.
    had_bids = 1 if price_sek else 0

    today_iso = date.today().isoformat()
    return {
        "tradera_id": raw["tradera_id"],
        "channel": "tradera",
        "url": raw.get("url"),
        "title": title,
        "raw_title": title,
        "brand": brand_matcher.match(title),
        "category": raw.get("category_label"),
        "size": extract_size(title),
        "item_type": raw.get("item_type", "Auction"),
        "final_price_sek": price_sek,
        "listed_price_sek": None,
        "bid_count": bid_count,
        "had_bids": had_bids,
        "ended_at": ended_at,
        "first_seen_at": today_iso,
        "last_seen_at": today_iso,
        "time_to_sell_days": None,
        "tradera_category_id": raw.get("tradera_category_id"),
        "scraped_at": datetime.utcnow().isoformat(),
    }
