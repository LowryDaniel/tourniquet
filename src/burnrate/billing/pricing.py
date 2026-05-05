"""Per-model pricing in pence per million tokens.

Update this table when Anthropic publishes price changes.
Source: https://www.anthropic.com/pricing (accessed 2026-05)
All rates converted to pence (GBP) at 1 USD ≈ 0.79 GBP.
"""

from decimal import Decimal

# (input_pence_per_million, output_pence_per_million)
_RATES: dict[str, tuple[Decimal, Decimal]] = {
    # Claude Opus 4.7
    "claude-opus-4-7": (Decimal("1185"), Decimal("5925")),
    # Claude Sonnet 4.6
    "claude-sonnet-4-6": (Decimal("237"), Decimal("1185")),
    # Claude Haiku 4.5
    "claude-haiku-4-5-20251001": (Decimal("63"), Decimal("315")),
    # Aliases / older model IDs that may appear in requests
    "claude-3-5-sonnet-20241022": (Decimal("237"), Decimal("1185")),
    "claude-3-5-haiku-20241022": (Decimal("63"), Decimal("315")),
}

_FALLBACK_RATE: tuple[Decimal, Decimal] = (Decimal("237"), Decimal("1185"))


def cost_pence(model: str, input_tokens: int, output_tokens: int) -> int:
    """Return cost in whole pence (rounded up) for a given model and token counts."""
    input_rate, output_rate = _RATES.get(model, _FALLBACK_RATE)
    million = Decimal("1_000_000")
    total = (input_rate * input_tokens + output_rate * output_tokens) / million
    return int(total.quantize(Decimal("1"), rounding="ROUND_UP"))
