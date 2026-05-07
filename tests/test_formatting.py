"""Tests for currency formatting utilities."""

import pytest

from tourniquet.billing.formatting import format_money, from_major_units


def test_format_usd():
    assert format_money(500, "USD") == "$5.00"


def test_format_gbp():
    # 500 USD cents × 0.79 = 395 pence = £3.95
    assert format_money(500, "GBP") == "£3.95"


def test_format_jpy():
    # 500 USD cents → $5.00 × 155 = 775 JPY (integer, no decimal)
    assert format_money(500, "JPY") == "¥775"


def test_format_zero():
    assert format_money(0, "USD") == "$0.00"


def test_from_major_units_usd():
    assert from_major_units(10.0, "USD") == 1000


def test_from_major_units_gbp_roundtrip():
    # 10 GBP / 0.79 * 100 = ~1265.8… → ceil → 1266
    result = from_major_units(10.0, "GBP")
    # Allow ±1 for rounding
    assert abs(result - 1266) <= 1


def test_unknown_currency_raises():
    with pytest.raises(ValueError, match="Unknown currency"):
        format_money(100, "XYZ")

    with pytest.raises(ValueError, match="Unknown currency"):
        from_major_units(10.0, "XYZ")
