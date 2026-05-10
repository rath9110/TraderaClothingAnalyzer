"""Unit tests for tradera/pricing.py — uses an in-memory SQLite fixture."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tradera.db import setup_db, upsert_items_batch
from tradera.pricing import build_lookups, predict_price


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    setup_db(conn)
    return conn


def _items(brand, category, size, channel, prices):
    """Build N normalized item dicts at a given price."""
    out = []
    for i, p in enumerate(prices):
        out.append({
            "tradera_id": f"{channel}-{brand}-{category}-{size}-{i}",
            "channel": channel,
            "url": "x", "title": "x", "raw_title": "x",
            "brand": brand, "category": category, "size": size,
            "item_type": "Auction",
            "final_price_sek": p if channel == "tradera" else None,
            "listed_price_sek": p if channel == "vinted" else None,
            "bid_count": None, "had_bids": 1,
            "ended_at": "2026-05-09",
            "first_seen_at": "2026-05-09",
            "last_seen_at": "2026-05-09",
            "time_to_sell_days": None,
            "tradera_category_id": 1629,
            "scraped_at": "2026-05-09T12:00:00",
        })
    return out


def test_lookup_finds_specific_match():
    conn = _make_conn()
    upsert_items_batch(conn, _items("Acne Studios", "dam_tröjor", "M", "tradera",
                                    list(range(100, 100 + 12))))
    lookups = build_lookups(conn)
    r = predict_price("Acne Studios", "dam_tröjor", "M", "tradera", lookups)
    assert r is not None
    assert r["granularity"] == "bcsc"
    assert r["n"] == 12
    assert 105 <= r["median"] <= 106


def test_falls_back_when_size_missing():
    conn = _make_conn()
    # 12 items at size M, 0 at size L
    upsert_items_batch(conn, _items("Acne Studios", "dam_tröjor", "M", "tradera",
                                    [200] * 12))
    lookups = build_lookups(conn)
    r = predict_price("Acne Studios", "dam_tröjor", "L", "tradera", lookups)
    assert r is not None
    # No size-L data, so falls back to brand × category × channel
    assert r["granularity"] == "bcc"


def test_returns_none_when_no_data_anywhere():
    conn = _make_conn()
    upsert_items_batch(conn, _items("Acne Studios", "dam_tröjor", "M", "tradera",
                                    [100] * 12))
    lookups = build_lookups(conn)
    r = predict_price("Unknown Brand", "unknown_cat", None, "tradera", lookups)
    assert r is None


def test_below_threshold_returns_low_confidence():
    """Low-n cells now return a result with confidence='low' instead of None."""
    conn = _make_conn()
    # only 5 items, below MIN_N=10
    upsert_items_batch(conn, _items("Acne Studios", "dam_tröjor", "M", "tradera",
                                    [100] * 5))
    lookups = build_lookups(conn)
    r = predict_price("Acne Studios", "dam_tröjor", "M", "tradera", lookups)
    assert r is not None
    assert r["confidence"] == "low"
    assert r["n"] == 5
    assert r["median"] == 100


def test_high_confidence_preferred_over_specificity():
    """When a coarser level has n>=10 but the specific level has n<10,
    we still prefer the SPECIFIC low-confidence match."""
    conn = _make_conn()
    # 5 items at brand+cat+size+channel (low conf, specific)
    upsert_items_batch(conn, _items("Acne Studios", "dam_tröjor", "M", "tradera",
                                    [100] * 5))
    # 12 items at brand+cat+channel (high conf, less specific) — different size
    upsert_items_batch(conn, _items("Acne Studios", "dam_tröjor", "L", "tradera",
                                    [500] * 12))
    lookups = build_lookups(conn)
    # When asked for size M: pass 1 (n>=10) finds nothing at M, finds size-L
    # at the bcsc level too — but bcsc keys differ by size, so M's bcsc has n=5,
    # L's bcsc has n=12.  Pass 1 walks levels for the M key:
    #   - bcsc(Acne|dam_tröjor|M|tradera) -> 5 items, fails MIN_N
    #   - bcc(Acne|dam_tröjor|tradera) -> 17 items combined → high-conf hit
    r = predict_price("Acne Studios", "dam_tröjor", "M", "tradera", lookups)
    assert r["confidence"] == "high"
    assert r["granularity"] == "bcc"  # fell back one level but stayed high-conf


def test_channels_separated():
    conn = _make_conn()
    upsert_items_batch(conn, _items("Acne Studios", "dam_tröjor", "M", "tradera",
                                    [400] * 12))
    upsert_items_batch(conn, _items("Acne Studios", "dam_tröjor", "M", "vinted",
                                    [200] * 12))
    lookups = build_lookups(conn)
    t = predict_price("Acne Studios", "dam_tröjor", "M", "tradera", lookups)
    v = predict_price("Acne Studios", "dam_tröjor", "M", "vinted", lookups)
    assert t["median"] == 400
    assert v["median"] == 200


def test_condition_aware_match_preferred():
    """When condition is supplied and condition-aware bucket has n>=10, prefer it."""
    conn = _make_conn()
    # 12 NWT items at 400, 12 VeryGood items at 200 — same brand/cat/size
    nwt = _items("Acne Studios", "dam_tröjor", "M", "vinted", [400] * 12)
    for it in nwt: it["condition"] = "NWT"
    vg = _items("Acne Studios", "dam_tröjor", "M", "vinted", [200] * 12)
    for it in vg: it["condition"] = "VeryGood"
    # Different tradera_ids so they don't collide
    for i, it in enumerate(vg): it["tradera_id"] = it["tradera_id"] + "-vg"
    upsert_items_batch(conn, nwt + vg)

    lookups = build_lookups(conn)
    nwt_pred = predict_price("Acne Studios", "dam_tröjor", "M", "vinted", lookups, condition="NWT")
    vg_pred  = predict_price("Acne Studios", "dam_tröjor", "M", "vinted", lookups, condition="VeryGood")
    assert nwt_pred["median"] == 400
    assert nwt_pred["granularity"] == "bcscZ"
    assert vg_pred["median"] == 200


def test_condition_falls_back_to_agnostic():
    """When the requested condition has too few samples, fall back to no-condition level."""
    conn = _make_conn()
    # Only 3 NWT items (below MIN_N), but 12 VeryGood
    nwt = _items("Acne Studios", "dam_tröjor", "M", "vinted", [400] * 3)
    for it in nwt: it["condition"] = "NWT"
    vg = _items("Acne Studios", "dam_tröjor", "M", "vinted", [200] * 12)
    for it in vg: it["condition"] = "VeryGood"
    for i, it in enumerate(vg): it["tradera_id"] = it["tradera_id"] + "-vg"
    upsert_items_batch(conn, nwt + vg)

    lookups = build_lookups(conn)
    r = predict_price("Acne Studios", "dam_tröjor", "M", "vinted", lookups, condition="NWT")
    # NWT has only 3 → no condition-aware level qualifies → falls to bcsc
    assert r["granularity"] == "bcsc"
    assert r["median"] == 200  # the no-condition median (dominated by VeryGood)


def test_parse_vinted_condition():
    from tradera.vinted import parse_vinted_condition
    assert parse_vinted_condition("S / 36 / 8 · Mycket bra") == "VeryGood"
    assert parse_vinted_condition("M · Ny med prislapp") == "NWT"
    assert parse_vinted_condition("L · Ny utan prislapp") == "NWOT"
    assert parse_vinted_condition("XL · Bra") == "Good"
    assert parse_vinted_condition("S · Tillfredsställande") == "Fair"
    assert parse_vinted_condition("S / 36") is None        # no condition delimiter
    assert parse_vinted_condition("") is None
    assert parse_vinted_condition("M · Unknown text") is None
