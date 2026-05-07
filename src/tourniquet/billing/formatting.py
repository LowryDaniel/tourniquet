"""Currency formatting utilities.

Canonical storage is USD cents. This module converts to display currencies.
"""

from __future__ import annotations

import math

# Static approximations — update periodically; not suitable for financial reporting.
_FX_FROM_USD: dict[str, float] = {
    "USD": 1.0,
    "GBP": 0.79,
    "EUR": 0.92,
    "JPY": 155.0,
    "CAD": 1.36,
    "AUD": 1.51,
}

_SYMBOLS: dict[str, str] = {
    "USD": "$",
    "GBP": "£",
    "EUR": "€",
    "JPY": "¥",
    "CAD": "C$",
    "AUD": "A$",
}

# JPY has no fractional minor unit; all others use 100 minor per major.
_MINOR_UNITS_PER_MAJOR: dict[str, int] = {
    "USD": 100,
    "GBP": 100,
    "EUR": 100,
    "JPY": 1,
    "CAD": 100,
    "AUD": 100,
}


def format_money(usd_cents: int, currency: str = "USD") -> str:
    """Convert USD cents to display string in the target currency.

    Examples:
        format_money(500, "USD") -> "$5.00"
        format_money(500, "GBP") -> "£3.95"
        format_money(500, "JPY") -> "¥775"
    """
    if currency not in _FX_FROM_USD:
        raise ValueError(f"Unknown currency: {currency!r}. Supported: {sorted(_FX_FROM_USD)}")

    symbol = _SYMBOLS[currency]
    fx = _FX_FROM_USD[currency]
    minor_per_major = _MINOR_UNITS_PER_MAJOR[currency]

    # usd_cents → usd_dollars → target_major_units
    target_major = (usd_cents / 100) * fx

    if minor_per_major == 1:
        # No fractional unit (JPY): round to nearest integer
        amount = round(target_major)
        return f"{symbol}{amount}"
    else:
        return f"{symbol}{target_major:.2f}"


def from_major_units(amount: float, currency: str = "USD") -> int:
    """Convert an amount in the currency's major unit to USD cents (integer).

    Examples:
        from_major_units(10.0, "USD") -> 1000
        from_major_units(10.0, "GBP") -> ~1266  (10 GBP / 0.79 * 100)
    """
    if currency not in _FX_FROM_USD:
        raise ValueError(f"Unknown currency: {currency!r}. Supported: {sorted(_FX_FROM_USD)}")

    fx = _FX_FROM_USD[currency]
    # major_units → USD dollars → USD cents
    usd_dollars = amount / fx
    return math.ceil(usd_dollars * 100)
