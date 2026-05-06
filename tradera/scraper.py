"""
Fetch and parse Tradera category pages for ended auctions.

Respects robots.txt: uses /en/category/ URLs (explicitly allowed).
?status=ended filter is not in the disallow list.
Rate-limits to 2–5 s between requests; caches raw HTML (7-day TTL).
"""
import gzip
import hashlib
import logging
import random
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger(__name__)

BASE_URL = "https://www.tradera.com"
CACHE_DIR = Path("data/raw_html_cache")
MIN_DELAY = 2.0
MAX_DELAY = 5.0
LOOKBACK_DAYS = 90
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


class SelectorDriftWarning(Exception):
    pass


class TraderaScraper:
    def __init__(self, cache_dir: Path = CACHE_DIR):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True)
        self._request_count = 0

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
        max_pages: int = 120,
    ) -> list[dict]:
        """
        Return raw item dicts for all ended auctions in the last 90 days
        for the given Tradera category ID.
        """
        cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
        items: list[dict] = []
        url: Optional[str] = f"{BASE_URL}/en/category/{category_id}?status=ended"
        page_num = 0

        while url and page_num < max_pages:
            page_num += 1
            log.info("  [%s] page %d — %s", category_label, page_num, url)

            html = self._fetch_page(url, category_id, page_num)
            if not html:
                log.warning("  [%s] failed to fetch page %d, stopping", category_label, page_num)
                break

            page_items, next_url = self._parse_listing_page(html, category_id, category_label)

            if page_num == 1 and not page_items:
                log.error(
                    "  [%s] zero items on page 1 — possible selector drift! "
                    "Check HTML structure at %s",
                    category_label,
                    url,
                )

            # Only keep items within the 90-day window
            in_window = [i for i in page_items if not i.get("end_text") or True]
            # Can't filter by date here (end_text needs parsing) — keep all,
            # parser.normalize_item will set ended_at; DB query enforces 90-day window.
            items.extend(page_items)

            # Early-stop heuristic: if we have a parseable end date on this page
            # and the oldest one is beyond cutoff, all subsequent pages will also be.
            # We detect this in the calling code after normalization.
            url = next_url
            if url:
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        log.info("  [%s] scraped %d raw items across %d pages", category_label, len(items), page_num)
        return items

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_page(self, url: str, category_id: int, page_num: int) -> Optional[str]:
        cache_key = hashlib.md5(url.encode()).hexdigest()[:16]
        cache_path = self.cache_dir / f"cat{category_id}_p{page_num:04d}_{cache_key}.html.gz"

        if cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < CACHE_TTL_SECONDS:
                with gzip.open(cache_path, "rt", encoding="utf-8") as f:
                    return f.read()

        self._request_count += 1
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
                log.warning("Rate-limited — sleeping 60 s before giving up on this page")
                time.sleep(60)
            return None
        except httpx.RequestError as e:
            log.warning("Request error for %s: %s", url, e)
            return None

    def _parse_listing_page(
        self, html: str, category_id: int, category_label: str
    ) -> tuple[list[dict], Optional[str]]:
        tree = HTMLParser(html)

        # Next-page URL from <link rel="next">
        next_link = tree.css_first('link[rel="next"]')
        next_url: Optional[str] = None
        if next_link:
            href = next_link.attributes.get("href", "")
            if href:
                next_url = href if href.startswith("http") else BASE_URL + href

        cards = tree.css('[id^="item-card-"]')
        items = []
        for card in cards:
            item = self._extract_item_card(card, category_id, category_label)
            if item:
                items.append(item)

        return items, next_url

    def _extract_item_card(
        self, card, category_id: int, category_label: str
    ) -> Optional[dict]:
        card_id = card.attributes.get("id", "")
        tradera_id = card_id.removeprefix("item-card-") if card_id else None
        if not tradera_id:
            return None

        # URL
        link = card.css_first('a[href*="/item/"]')
        url: Optional[str] = None
        if link:
            href = link.attributes.get("href", "")
            url = href if href.startswith("http") else BASE_URL + href

        # Title — stable: try data-testid first, then class containing "title"
        title_node = (
            card.css_first('[data-testid="title"]')
            or card.css_first('[class*="__title"]')
            or card.css_first('[class*="title"]')
        )
        title = title_node.text(strip=True) if title_node else ""

        # Price
        price_node = card.css_first('[data-testid="price"]')
        price_text = price_node.text(strip=True) if price_node else ""

        # Bids
        bids_node = card.css_first('[data-testid="bids-label"]')
        bids_text = bids_node.text(strip=True) if bids_node else ""

        # Item type
        item_type = card.attributes.get("data-item-type", "Auction")

        # End-time text — look for time, or elements containing Swedish date keywords
        time_node = (
            card.css_first("time")
            or card.css_first('[class*="time"]')
            or card.css_first('[class*="ending"]')
            or card.css_first('[class*="ended"]')
            or card.css_first('[class*="Ending"]')
        )
        end_text = time_node.text(strip=True) if time_node else ""

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
