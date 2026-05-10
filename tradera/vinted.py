"""
Vinted.se scraper.

Vinted does not expose a "sold" filter or listing dates publicly, so we
capture ACTIVE listings only.  Time-to-sell becomes available longitudinally:
items present this week but missing next week are treated as sold, with
time_to_sell_days = last_seen_at − first_seen_at.

Vinted's robots.txt blocks AI crawlers (GPTBot, ClaudeBot, etc.) but allows
generic crawlers under the default `*` rule.  We use a respectful User-Agent
and 2–5 s delays between requests.
"""
import gzip
import hashlib
import logging
import random
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import httpx
from selectolax.parser import HTMLParser

from tradera.parser import BrandMatcher, extract_size

log = logging.getLogger(__name__)

BASE_URL = "https://www.vinted.se"
CACHE_DIR = Path("data/raw_html_cache_vinted")
MIN_DELAY = 2.0
MAX_DELAY = 5.0
CACHE_TTL_SECONDS = 7 * 86400
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

# Match "100,00 kr" or "100 kr" — Swedish decimal comma
RE_VINTED_PRICE = re.compile(r"([\d\s]+)(?:[,.](\d{1,2}))?\s*kr", re.IGNORECASE)
# Strict top-level item card testid: product-item-id-NNN  (no --suffix)
RE_TOP_LEVEL_CARD = re.compile(r"^product-item-id-\d+$")

# Vinted condition vocabulary (Swedish → canonical English).
# Ordered roughly from new to worst; appears as the substring after " · "
# in the item subtitle, e.g. "S / 36 / 8 · Mycket bra".
VINTED_CONDITIONS = {
    "ny med prislapp":   "NWT",        # New with tag
    "ny utan prislapp":  "NWOT",       # New without tag
    "mycket bra":        "VeryGood",
    "bra":               "Good",
    "tillfredsställande": "Fair",
}


def parse_vinted_condition(subtitle: str) -> Optional[str]:
    """
    Extract canonical condition code from a Vinted subtitle string.
    Returns None if no recognised condition is found.

    Examples:
      "S / 36 / 8 · Mycket bra"   → "VeryGood"
      "M · Ny med prislapp"        → "NWT"
      "L"                          → None
    """
    if not subtitle or "·" not in subtitle:
        return None
    tail = subtitle.rsplit("·", 1)[-1].strip().lower()
    return VINTED_CONDITIONS.get(tail)


class VintedScraper:
    def __init__(self, cache_dir: Path = CACHE_DIR, use_cache: bool = True):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.use_cache = use_cache
        self.client = httpx.Client(headers=HEADERS, timeout=60, follow_redirects=True)

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def scrape_category(
        self,
        category_id: int,
        category_label: str,
        slug: str,
        max_pages: int = 10,
    ) -> list[dict]:
        """
        Return raw item dicts for ACTIVE listings.  Vinted requires the
        category slug in the URL — slugless URLs are redirected to a 204.
        """
        items: list[dict] = []
        for page_num in range(1, max_pages + 1):
            url = f"{BASE_URL}/catalog/{category_id}-{slug}?page={page_num}"
            log.info("  [vinted/%s] page %d", category_label, page_num)

            html = self._fetch_page(url, category_id, page_num)
            if not html:
                log.warning("  [vinted/%s] failed page %d, stopping", category_label, page_num)
                break

            page_items = self._parse_listing_page(html, category_id, category_label)
            if page_num == 1 and not page_items:
                log.error(
                    "  [vinted/%s] zero items on page 1 — selector drift or block. URL: %s",
                    category_label,
                    url,
                )
                break

            if not page_items:
                # End of pages
                break

            items.extend(page_items)
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        log.info("  [vinted/%s] %d items captured", category_label, len(items))
        return items

    # ------------------------------------------------------------------

    def _fetch_page(self, url: str, category_id: int, page_num: int) -> Optional[str]:
        cache_key = hashlib.md5(url.encode()).hexdigest()[:16]
        cache_path = self.cache_dir / f"vcat{category_id}_p{page_num:04d}_{cache_key}.html.gz"

        if self.use_cache and cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < CACHE_TTL_SECONDS:
                with gzip.open(cache_path, "rt", encoding="utf-8") as f:
                    return f.read()

        try:
            resp = self.client.get(url)
            resp.raise_for_status()
            html = resp.text
            with gzip.open(cache_path, "wt", encoding="utf-8") as f:
                f.write(html)
            return html
        except httpx.HTTPStatusError as e:
            log.warning("HTTP %s for %s", e.response.status_code, url)
            if e.response.status_code in (403, 429):
                log.warning("Rate-limited / forbidden — sleeping 60 s")
                time.sleep(60)
            return None
        except httpx.RequestError as e:
            log.warning("Request error for %s: %s", url, e)
            return None

    def _parse_listing_page(
        self, html: str, category_id: int, category_label: str
    ) -> list[dict]:
        tree = HTMLParser(html)
        items = []
        for card in tree.css('[data-testid^="product-item-id-"]'):
            tid = card.attributes.get("data-testid", "")
            if not RE_TOP_LEVEL_CARD.match(tid):
                continue
            item = self._extract_item_card(card, category_id, category_label)
            if item:
                items.append(item)
        return items

    def _extract_item_card(self, card, category_id: int, category_label: str) -> Optional[dict]:
        tid = card.attributes.get("data-testid", "")
        platform_id = tid.removeprefix("product-item-id-")
        if not platform_id:
            return None

        # Brand
        brand_node = card.css_first('[data-testid$="--description-title"]')
        brand_text = brand_node.text(strip=True) if brand_node else ""

        # Size + condition
        sub_node = card.css_first('[data-testid$="--description-subtitle"]')
        subtitle = sub_node.text(strip=True) if sub_node else ""

        # Price
        price_node = card.css_first('[data-testid$="--price-text"]')
        price_text = price_node.text(strip=True) if price_node else ""

        # URL
        link = card.css_first('a[href*="/items/"]')
        url = None
        full_title = ""
        if link:
            href = link.attributes.get("href", "")
            url = href if href.startswith("http") else BASE_URL + href
            full_title = link.attributes.get("title", "").strip()

        # Build a unified `title` field by combining brand and subtitle so the
        # downstream BrandMatcher / size extractor work without changes.
        title = f"{brand_text} {subtitle}".strip() or full_title

        return {
            "platform_id": platform_id,
            "url": url,
            "title": title,
            "raw_title": full_title or title,
            "brand_text": brand_text,
            "subtitle": subtitle,
            "price_text": price_text,
            "tradera_category_id": category_id,
            "category_label": category_label,
        }


def parse_vinted_price(text: str) -> Optional[int]:
    """'100,00 kr' → 100, '1 250,50 kr' → 1250 — round to whole SEK."""
    if not text:
        return None
    m = RE_VINTED_PRICE.search(text)
    if not m:
        return None
    whole = re.sub(r"\s+", "", m.group(1))
    try:
        return int(whole)
    except ValueError:
        return None


def normalize_vinted_item(raw: dict, brand_matcher: BrandMatcher) -> dict:
    """Convert a raw Vinted scraper dict to a DB-ready record."""
    listed_price = parse_vinted_price(raw.get("price_text", ""))
    title = raw.get("title", "")

    # Vinted gives us an explicit brand — prefer it, but canonicalise via
    # our whitelist when possible (so "ASOS Design" → "ASOS Design", "h&m" → "H&M").
    explicit_brand = (raw.get("brand_text") or "").strip()
    canonical = brand_matcher.match(title) or brand_matcher.match(explicit_brand)
    brand = canonical or (explicit_brand if explicit_brand else None)

    today_iso = date.today().isoformat()
    now_iso = datetime.utcnow().isoformat()

    return {
        "tradera_id": f"v{raw['platform_id']}",   # prefix to avoid PK collision
        "channel": "vinted",
        "url": raw.get("url"),
        "title": title,
        "raw_title": raw.get("raw_title") or title,
        "brand": brand,
        "category": raw.get("category_label"),
        "size": extract_size(raw.get("subtitle", "")) or extract_size(title),
        "condition": parse_vinted_condition(raw.get("subtitle", "")),
        "item_type": "BuyNow",
        # Vinted is fixed-price: listing price = transaction price if it sells
        "final_price_sek": None,           # only set when item disappears (sold)
        "listed_price_sek": listed_price,
        "bid_count": None,
        "had_bids": 0,                     # 0 until we observe disappearance
        "ended_at": None,                  # set when item disappears
        "first_seen_at": today_iso,
        "last_seen_at": today_iso,
        "time_to_sell_days": None,
        "tradera_category_id": raw.get("tradera_category_id"),
        "scraped_at": now_iso,
    }


def backfill_conditions_from_cache(conn, cache_dir: Path = CACHE_DIR) -> int:
    """
    Re-parse cached Vinted HTML and UPDATE the `condition` column for
    existing rows.  Does NOT touch first_seen_at / last_seen_at — purely
    a backfill of the new field.  Returns count updated.
    """
    files = sorted(cache_dir.glob("*.html.gz"))
    if not files:
        log.warning("No Vinted cache files found.")
        return 0

    updated = 0
    for f in files:
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            html = fh.read()
        tree = HTMLParser(html)
        for card in tree.css('[data-testid^="product-item-id-"]'):
            tid = card.attributes.get("data-testid", "")
            if not RE_TOP_LEVEL_CARD.match(tid):
                continue
            platform_id = tid.removeprefix("product-item-id-")
            sub_node = card.css_first('[data-testid$="--description-subtitle"]')
            subtitle = sub_node.text(strip=True) if sub_node else ""
            condition = parse_vinted_condition(subtitle)
            if condition:
                cur = conn.execute(
                    "UPDATE items SET condition = ? WHERE tradera_id = ? AND condition IS NULL",
                    (condition, f"v{platform_id}"),
                )
                updated += cur.rowcount
    conn.commit()
    return updated
