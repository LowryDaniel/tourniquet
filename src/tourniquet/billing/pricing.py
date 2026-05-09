"""Per-model pricing in USD cents per million tokens.

Update this table when Anthropic publishes price changes.
Source: https://www.anthropic.com/pricing (accessed 2026-05)
All rates in USD cents (Anthropic's canonical currency).
"""

import logging
from decimal import Decimal

log = logging.getLogger(__name__)

# (input_cents_per_million, output_cents_per_million)
_RATES: dict[str, tuple[Decimal, Decimal]] = {
    # Claude Opus 4.7: $15/$75 per M tokens
    "claude-opus-4-7": (Decimal("1500"), Decimal("7500")),
    # Claude Sonnet 4.6: $3/$15 per M tokens
    "claude-sonnet-4-6": (Decimal("300"), Decimal("1500")),
    # Claude Haiku 4.5: $0.80/$4 per M tokens
    "claude-haiku-4-5-20251001": (Decimal("80"), Decimal("400")),
    # Aliases / older model IDs that may appear in requests
    "claude-3-5-sonnet-20241022": (Decimal("300"), Decimal("1500")),
    "claude-3-5-haiku-20241022": (Decimal("80"), Decimal("400")),
    # TODO: add `claude-haiku-4-7` placeholder once Anthropic's roadmap
    # confirms it (see code-review-remediation.md M1, item 3).
}

# Pessimistic fallback: bill unknown models at the most expensive rate in
# `_RATES` so caps fire earlier (not later) and revenue/cost fidelity errs
# on the safe side. Keep this in sync with the most-expensive model above.
_FALLBACK_RATE: tuple[Decimal, Decimal] = _RATES["claude-opus-4-7"]

# Dedup set so we only log a warning once per unknown model name per process.
_UNKNOWN_MODELS_LOGGED: set[str] = set()


def cost_usd_cents(model: str, input_tokens: int, output_tokens: int) -> int:
    """Return cost in whole USD cents (rounded up) for a given model and token counts."""
    if model not in _RATES:
        if model not in _UNKNOWN_MODELS_LOGGED:
            log.warning(
                "Unknown model %r — billing at fallback rate. Update pricing.py.",
                model,
            )
            _UNKNOWN_MODELS_LOGGED.add(model)
    input_rate, output_rate = _RATES.get(model, _FALLBACK_RATE)
    million = Decimal("1_000_000")
    total = (input_rate * input_tokens + output_rate * output_tokens) / million
    return int(total.quantize(Decimal("1"), rounding="ROUND_UP"))
