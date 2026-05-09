import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/tradera.db")
SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS items (
    tradera_id      TEXT PRIMARY KEY,
    channel         TEXT NOT NULL DEFAULT 'tradera',
    url             TEXT,
    title           TEXT,
    raw_title       TEXT,
    brand           TEXT,
    category        TEXT,
    size            TEXT,
    item_type       TEXT,
    final_price_sek INTEGER,
    listed_price_sek INTEGER,
    bid_count       INTEGER,
    had_bids        INTEGER,
    ended_at        TEXT,
    first_seen_at   TEXT,
    last_seen_at    TEXT,
    time_to_sell_days INTEGER,
    tradera_category_id INTEGER,
    scraped_at      TEXT
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT,
    completed_at        TEXT,
    channel             TEXT,
    categories_scraped  TEXT,
    items_upserted      INTEGER,
    status              TEXT
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_items_brand_cat ON items(brand, category, ended_at);
CREATE INDEX IF NOT EXISTS ix_items_ended ON items(ended_at);
CREATE INDEX IF NOT EXISTS ix_items_channel ON items(channel);
CREATE INDEX IF NOT EXISTS ix_items_last_seen ON items(channel, last_seen_at);
"""

# Idempotent column-add migrations for the multi-channel rollout
MIGRATIONS = [
    ("items", "channel", "TEXT NOT NULL DEFAULT 'tradera'"),
    ("items", "listed_price_sek", "INTEGER"),
    ("items", "first_seen_at", "TEXT"),
    ("items", "last_seen_at", "TEXT"),
    ("items", "time_to_sell_days", "INTEGER"),
    ("scrape_runs", "channel", "TEXT"),
]


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def setup_db(conn: sqlite3.Connection) -> None:
    # 1. Tables first (CREATE TABLE IF NOT EXISTS won't add columns to old DBs)
    conn.executescript(SCHEMA_TABLES)

    # 2. Additive column migrations on existing DBs (no-op on fresh)
    for table, column, ddl in MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # 3. Indexes — must run after migrations so columns referenced by
    #    composite indexes (e.g. last_seen_at) exist.
    conn.executescript(SCHEMA_INDEXES)

    # 4. Backfill timestamps for legacy Tradera rows.
    conn.execute("UPDATE items SET first_seen_at = scraped_at WHERE first_seen_at IS NULL")
    conn.execute("UPDATE items SET last_seen_at = scraped_at WHERE last_seen_at IS NULL")
    conn.commit()


def upsert_item(conn: sqlite3.Connection, item: dict) -> None:
    """
    Upsert an item.  On UPDATE: preserve `first_seen_at`, refresh
    `last_seen_at`.  On INSERT: set both to scrape time.
    """
    item = dict(item)
    item.setdefault("listed_price_sek", None)
    item.setdefault("time_to_sell_days", None)
    item.setdefault("channel", "tradera")
    now = item.get("scraped_at")
    item.setdefault("first_seen_at", now)
    item.setdefault("last_seen_at", now)

    conn.execute(
        """
        INSERT INTO items (
            tradera_id, channel, url, title, raw_title, brand, category, size,
            item_type, final_price_sek, listed_price_sek, bid_count, had_bids,
            ended_at, first_seen_at, last_seen_at, time_to_sell_days,
            tradera_category_id, scraped_at
        ) VALUES (
            :tradera_id, :channel, :url, :title, :raw_title, :brand, :category, :size,
            :item_type, :final_price_sek, :listed_price_sek, :bid_count, :had_bids,
            :ended_at, :first_seen_at, :last_seen_at, :time_to_sell_days,
            :tradera_category_id, :scraped_at
        )
        ON CONFLICT(tradera_id) DO UPDATE SET
            channel          = excluded.channel,
            url              = excluded.url,
            title            = excluded.title,
            brand            = excluded.brand,
            category         = excluded.category,
            size             = excluded.size,
            item_type        = excluded.item_type,
            final_price_sek  = excluded.final_price_sek,
            listed_price_sek = excluded.listed_price_sek,
            bid_count        = excluded.bid_count,
            had_bids         = excluded.had_bids,
            ended_at         = excluded.ended_at,
            last_seen_at     = excluded.last_seen_at,
            scraped_at       = excluded.scraped_at
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


def log_run_start(conn: sqlite3.Connection, categories: list[str], channel: str = "tradera") -> int:
    import json
    cur = conn.execute(
        "INSERT INTO scrape_runs (started_at, channel, categories_scraped, status) VALUES (?, ?, ?, 'running')",
        (datetime.utcnow().isoformat(), channel, json.dumps(categories)),
    )
    conn.commit()
    return cur.lastrowid


def log_run_complete(conn: sqlite3.Connection, run_id: int, items_upserted: int, status: str = "success") -> None:
    conn.execute(
        "UPDATE scrape_runs SET completed_at = ?, items_upserted = ?, status = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), items_upserted, status, run_id),
    )
    conn.commit()


def mark_disappeared_items_sold(conn: sqlite3.Connection, channel: str, run_started_at: str) -> int:
    """
    For Vinted: items that exist in the DB but were NOT seen in this run
    have likely sold (or been withdrawn).  Compute time_to_sell_days from
    first_seen_at to last_seen_at and finalise.

    Only applies to items not yet marked sold (time_to_sell_days IS NULL).
    Returns count updated.
    """
    cur = conn.execute(
        """
        UPDATE items
        SET time_to_sell_days = CAST(julianday(last_seen_at) - julianday(first_seen_at) AS INTEGER),
            ended_at = COALESCE(ended_at, last_seen_at),
            had_bids = 1
        WHERE channel = ?
          AND time_to_sell_days IS NULL
          AND last_seen_at < ?
          AND first_seen_at < last_seen_at
        """,
        (channel, run_started_at),
    )
    conn.commit()
    return cur.rowcount
