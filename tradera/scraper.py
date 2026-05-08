"""
Fetch and parse Tradera category pages for sold (ended) auctions.

Tradera's `?itemStatus=Ended` filter (note the capital E) returns items that
ended AND received a sale — this is "sold only" data. Listing cards expose:
  - title (from <a aria-label="...">)
  - final sale price (data-testid="price", units in SEK)
  - end-status text ("Ended" — no exact date on listing)
  - item type via data-item-type

Bid count is NOT exposed on the listing for ended items; would require a
per-item detail-page request and is out of scope for v1.
"""
import gzip
import hashlib
import logging
import random
import time
from pathlib import Path
from typing import Optional

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger(__name__)

BASE_URL = "https://www.tradera.com"
CACHE_DIR = Path("data/raw_html_cache")
MIN_DELAY = 2.0
MAX_DELAY = 5.0
CACHE_TTL_SECONDS = 7 * 86400
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; TraderaVelocityAnalyzer/1.0; "
        "personal-research-tool; single-user-weekly-batch)"
    ),
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

# Item types we want — placeholders/non-items have data-item-type missing or '?'
VALID_ITEM_TYPES = {"Auction", "AuctionBin", "PureBin", "ShopItem"}


class TraderaScraper:
    def __init__(self, cache_dir: Path = CACHE_DIR, use_cache: bool = True):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.use_cache = use_cache
        self.client = httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True)

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
        max_pages: int = 30,
    ) -> list[dict]:
        items: list[dict] = []
        url: Optional[str] = f"{BASE_URL}/en/category/{category_id}?itemStatus=Ended"
        page_num = 0

        while url and page_num < max_pages:
            page_num += 1
            log.info("  [%s] page %d", category_label, page_num)

            html = self._fetch_page(url, category_id, page_num)
            if not html:
                log.warning("  [%s] failed to fetch page %d, stopping", category_label, page_num)
                break

            page_items, next_url = self._parse_listing_page(html, category_id, category_label)

            if page_num == 1 and not page_items:
                log.error(
                    "  [%s] zero items on page 1 — possible selector drift or filter change. URL: %s",
                    category_label,
                    url,
                )

            items.extend(page_items)
            url = next_url
            if url:
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        log.info("  [%s] %d sold items across %d pages", category_label, len(items), page_num)
        return items

    # ------------------------------------------------------------------

    def _fetch_page(self, url: str, category_id: int, page_num: int) -> Optional[str]:
        cache_key = hashlib.md5(url.encode()).hexdigest()[:16]
        cache_path = self.cache_dir / f"cat{category_id}_p{page_num:04d}_{cache_key}.html.gz"

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
            if e.response.status_code == 429:
                log.warning("Rate-limited — sleeping 60 s")
                time.sleep(60)
            return None
        except httpx.RequestError as e:
            log.warning("Request error for %s: %s", url, e)
            return None

    def _parse_listing_page(
        self, html: str, category_id: int, category_label: str
    ) -> tuple[list[dict], Optional[str]]:
        tree = HTMLParser(html)

        next_link = tree.css_first('link[rel="next"]')
        next_url: Optional[str] = None
        if next_link:
            href = next_link.attributes.get("href", "")
            if href:
                next_url = href if href.startswith("http") else BASE_URL + href

        items = []
        for card in tree.css('[id^="item-card-"]'):
            item_type = card.attributes.get("data-item-type")
            if item_type not in VALID_ITEM_TYPES:
                continue
            item = self._extract_item_card(card, category_id, category_label, item_type)
            if item:
                items.append(item)

        return items, next_url

    def _extract_item_card(
        self, card, category_id: int, category_label: str, item_type: str
    ) -> Optional[dict]:
        card_id_attr = card.attributes.get("id", "")
        tradera_id = card_id_attr.removeprefix("item-card-")
        if not tradera_id:
            return None

        # URL + title via the main item link
        link = card.css_first('a[href*="/item/"]')
        url: Optional[str] = None
        title = ""
        if link:
            href = link.attributes.get("href", "")
            url = href if href.startswith("http") else BASE_URL + href
            title = (
                link.attributes.get("aria-label")
                or link.attributes.get("title")
                or ""
            ).strip()

        # Fallback: look for a textTruncate node in the card if aria-label was empty
        if not title:
            t = card.css_first('[class*="textTruncate"]')
            title = t.text(strip=True) if t else ""

        # Price (final sale price for ended items)
        price_node = card.css_first('[data-testid="price"]')
        price_text = price_node.text(strip=True) if price_node else ""

        # End status text — for ended items just shows "Ended"
        time_node = card.css_first(f"#item-card-{tradera_id}-time")
        end_text = time_node.text(strip=True) if time_node else ""

        # Bid count is not exposed on listing for ended items
        bids_node = card.css_first('[data-testid="bids-label"]')
        bids_text = bids_node.text(strip=True) if bids_node else ""

        return {
            "tradera_id": tradera_id,
            "url": url,
            "title": title,
            "raw_title": title,
            "item_type": item_type,
            "price_text": price_text,
            "bids_text": bids_text,
            "end_text": end_text,
            "tradera_category_id": category_id,
            "category_label": category_label,
        }
