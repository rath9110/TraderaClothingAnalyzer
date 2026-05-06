import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/tradera.db")
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    tradera_id      TEXT PRIMARY KEY,
    url             TEXT,
    title           TEXT,
    raw_title       TEXT,
    brand           TEXT,
    category        TEXT,
    size            TEXT,
    item_type       TEXT,
    final_price_sek INTEGER,
    bid_count       INTEGER,
    had_bids        INTEGER,
    ended_at        TEXT,
    tradera_category_id INTEGER,
    scraped_at      TEXT
);

CREATE INDEX IF NOT EXISTS ix_items_brand_cat
    ON items(brand, category, ended_at);

CREATE INDEX IF NOT EXISTS ix_items_ended
    ON items(ended_at);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT,
    completed_at        TEXT,
    categories_scraped  TEXT,
    items_upserted      INTEGER,
    status              TEXT
);
"""


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def setup_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def upsert_item(conn: sqlite3.Connection, item: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO items (
            tradera_id, url, title, raw_title, brand, category, size,
            item_type, final_price_sek, bid_count, had_bids,
            ended_at, tradera_category_id, scraped_at
        ) VALUES (
            :tradera_id, :url, :title, :raw_title, :brand, :category, :size,
            :item_type, :final_price_sek, :bid_count, :had_bids,
            :ended_at, :tradera_category_id, :scraped_at
        )
        """,
        item,
    )


def upsert_items_batch(conn: sqlite3.Connection, items: list[dict]) -> int:
    for item in items:
        upsert_item(conn, item)
    conn.commit()
    return len(items)


def get_items_last_n_days(conn: sqlite3.Connection, days: int = 90) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM items
        WHERE ended_at >= date('now', ? || ' days')
        ORDER BY ended_at DESC
        """,
        (f"-{days}",),
    ).fetchall()


def log_run_start(conn: sqlite3.Connection, categories: list[str]) -> int:
    import json
    cur = conn.execute(
        "INSERT INTO scrape_runs (started_at, categories_scraped, status) VALUES (?, ?, 'running')",
        (datetime.utcnow().isoformat(), json.dumps(categories)),
    )
    conn.commit()
    return cur.lastrowid


def log_run_complete(conn: sqlite3.Connection, run_id: int, items_upserted: int, status: str = "success") -> None:
    conn.execute(
        "UPDATE scrape_runs SET completed_at = ?, items_upserted = ?, status = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), items_upserted, status, run_id),
    )
    conn.commit()
