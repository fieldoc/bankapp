from decimal import Decimal

import pytest

from bankapp import money


def test_to_minor_basic():
    assert money.to_minor("12.34", "CAD") == 1234
    assert money.to_minor("0.00", "CAD") == 0
    assert money.to_minor("-5.00", "CAD") == -500
    assert money.to_minor("1000", "CAD") == 100000


def test_to_minor_accepts_decimal_and_int():
    assert money.to_minor(Decimal("12.34"), "CAD") == 1234
    assert money.to_minor(1000, "CAD") == 100000


def test_to_minor_rejects_float():
    with pytest.raises(TypeError):
        money.to_minor(12.34, "CAD")


def test_to_minor_rejects_excess_precision():
    with pytest.raises(ValueError):
        money.to_minor("12.345", "CAD")  # sub-cent for a 2-exponent currency


def test_exponent_map():
    assert money.exponent_for("CAD") == 2
    assert money.exponent_for("USD") == 2
    assert money.exponent_for("BTC") == 8
    assert money.exponent_for("XYZ") == 2  # default


def test_btc_exponent():
    assert money.to_minor("1.00000001", "BTC") == 100000001


def test_from_minor_roundtrip():
    assert money.from_minor(1234, "CAD") == Decimal("12.34")
    assert money.from_minor(-500, "CAD") == Decimal("-5.00")
    assert money.from_minor(100000001, "BTC") == Decimal("1.00000001")


def test_share_split_even():
    assert money.share_split(240000, 1, 2) == (120000, 120000)


def test_share_split_odd_cent_floors_my_share():
    # my share floors; the extra cent lands on the remainder (the receivable)
    assert money.share_split(240001, 1, 2) == (120000, 120001)


def test_share_split_sums_to_total():
    my, rest = money.share_split(99999, 1, 3)
    assert my + rest == 99999
    assert my == 99999 // 3
