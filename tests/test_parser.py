"""Unit tests for tradera/parser.py — run with: python -m pytest tests/"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tradera.parser import extract_size, parse_price, parse_bid_count, parse_end_date


def test_extract_size_letter():
    assert extract_size("Acne Studios stickad tröja M mörkblå") == "M"
    assert extract_size("Filippa K blus XS vit") == "XS"
    assert extract_size("Jacka XL herr") == "XL"


def test_extract_size_numeric():
    assert extract_size("Klänning storlek 38 grön") == "38"
    assert extract_size("Byxor stl 42") == "42"


def test_extract_size_stl_prefix():
    assert extract_size("stl. M fin skjorta") == "M"
    assert extract_size("stl 40 kostym") == "40"


def test_extract_size_jeans():
    assert extract_size("Levi's 501 28/32 blå") == "28/32"
    assert extract_size("Nudie jeans 30x30") == "30/30"


def test_extract_size_none():
    assert extract_size("Gucci väska svart läder") is None
    assert extract_size("Patagonia fleece") is None


def test_parse_price():
    assert parse_price("360 kr") == 360
    assert parse_price("1 200 kr") == 1200
    assert parse_price("50kr") == 50
    assert parse_price("") is None
    assert parse_price("Gratis") is None


def test_parse_bid_count():
    count, had = parse_bid_count("5 bud")
    assert count == 5 and had == 1

    count, had = parse_bid_count("0 bud")
    assert count == 0 and had == 0

    count, had = parse_bid_count("Inga bud")
    assert count == 0 and had == 0

    count, had = parse_bid_count("")
    assert count is None and had == 0


def test_parse_end_date_simple():
    result = parse_end_date("10 maj 23:10")
    assert result is not None
    assert result.endswith("-05-10")


def test_parse_end_date_with_year():
    result = parse_end_date("15 mars 2025")
    assert result == "2025-03-15"


def test_parse_end_date_idag():
    from datetime import date
    result = parse_end_date("Idag 15:30")
    assert result == date.today().isoformat()


def test_parse_end_date_none():
    assert parse_end_date("") is None
    assert parse_end_date("Avslutad") is None
