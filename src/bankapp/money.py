"""Money as INTEGER minor units. Float is banned; Decimal only at the boundary.

Amounts are stored as signed integer minor units (cents for CAD/USD, satoshis for
BTC). Parsing happens here with ``decimal.Decimal`` and nowhere else, so exactness,
sortability, and native SQL ``SUM()`` all hold.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Union

# Per-currency exponent (number of minor units per major unit, as a power of ten).
_EXPONENTS = {
    "CAD": 2,
    "USD": 2,
    "BTC": 8,
}
_DEFAULT_EXPONENT = 2

Number = Union[str, int, Decimal]


def exponent_for(currency: str) -> int:
    """Minor-unit exponent for a currency (CAD/USD=2, BTC=8, default 2)."""
    return _EXPONENTS.get(currency.upper(), _DEFAULT_EXPONENT)


def known_currencies() -> tuple[str, ...]:
    """Currencies with a defined minor-unit exponent, sorted.

    ``exponent_for`` silently falls back to two places for an unrecognized code, so
    any caller accepting user input must gate on this allowlist first — otherwise a
    typo'd currency yields a goal matching no transactions, reading as 0% funded
    forever with no error raised anywhere.
    """
    return tuple(sorted(_EXPONENTS))


def to_minor(value: Number, currency: str) -> int:
    """Convert a money value to signed integer minor units.

    Accepts ``str``, ``int``, or ``Decimal``. Floats are rejected outright to keep
    binary rounding error out of the ledger. Values with more precision than the
    currency allows (e.g. sub-cent CAD) raise ``ValueError``.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"float is banned for money; pass str/Decimal/int, got {type(value).__name__}")
    d = value if isinstance(value, Decimal) else Decimal(str(value) if isinstance(value, int) else value)
    exp = exponent_for(currency)
    scaled = d * (Decimal(10) ** exp)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{value!r} has more precision than {currency} allows ({exp} places)")
    return int(scaled)


def from_minor(minor: int, currency: str) -> Decimal:
    """Convert signed integer minor units back to a Decimal major-unit value."""
    exp = exponent_for(currency)
    return (Decimal(minor) / (Decimal(10) ** exp)).quantize(Decimal(10) ** -exp)


def share_split(total_minor: int, numer: int, denom: int) -> tuple[int, int]:
    """Split ``total_minor`` into (my_share, remainder).

    My share floors (``total * numer // denom``); the remainder carries any odd
    minor unit, so the two always sum back to ``total_minor`` exactly. In the
    split-expense model the remainder is the roommate's receivable.
    """
    my_share = (total_minor * numer) // denom
    remainder = total_minor - my_share
    return my_share, remainder
