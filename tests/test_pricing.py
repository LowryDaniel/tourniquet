"""Pricing calculations — USD cents integer, no float drift."""

import logging

from tourniquet.billing.pricing import _UNKNOWN_MODELS_LOGGED, cost_usd_cents


def test_zero_tokens():
    assert cost_usd_cents("claude-sonnet-4-6", 0, 0) == 0


def test_sonnet_1m_input():
    # Sonnet 4.6: $3/1M input = 300 cents
    assert cost_usd_cents("claude-sonnet-4-6", 1_000_000, 0) == 300


def test_sonnet_1m_output():
    # Sonnet 4.6: $15/1M output = 1500 cents
    assert cost_usd_cents("claude-sonnet-4-6", 0, 1_000_000) == 1500


def test_rounds_up():
    # Tiny request: 1 input + 1 output — well below 1 cent, rounds up to 1
    result = cost_usd_cents("claude-sonnet-4-6", 1, 1)
    assert result >= 1
    assert isinstance(result, int)


def test_opus_more_expensive_than_sonnet():
    sonnet = cost_usd_cents("claude-sonnet-4-6", 1_000_000, 1_000_000)
    opus = cost_usd_cents("claude-opus-4-7", 1_000_000, 1_000_000)
    assert opus > sonnet


def test_unknown_model_uses_pessimistic_fallback():
    # Unknown models must bill at Opus rates (most expensive), not Sonnet.
    # Opus 4.7: $15/$75 per M tokens => 1_000_000 in + 1_000_000 out = 9000 cents.
    # Sonnet 4.6 would be 1800 cents — confirm we're NOT using Sonnet.
    _UNKNOWN_MODELS_LOGGED.discard("claude-opus-99")
    cost = cost_usd_cents("claude-opus-99", 1_000_000, 1_000_000)
    assert cost == 9000  # Opus rate
    assert cost > 1800  # not Sonnet rate


def test_unknown_model_logs_warning_once(caplog):
    # Reset dedupe state so the warning will fire on the first call below.
    _UNKNOWN_MODELS_LOGGED.discard("claude-mystery-1")
    with caplog.at_level(logging.WARNING, logger="tourniquet.billing.pricing"):
        cost_usd_cents("claude-mystery-1", 100, 100)
        cost_usd_cents("claude-mystery-1", 200, 200)
        cost_usd_cents("claude-mystery-1", 300, 300)
    matching = [r for r in caplog.records if "claude-mystery-1" in r.getMessage()]
    assert len(matching) == 1


# ── Cache-tier pricing (Anthropic prompt caching) ────────────────────────────
# Anthropic prices cache tokens off the model's base *input* rate:
#   - cache_creation_input_tokens: 1.25× input rate (write premium)
#   - cache_read_input_tokens:     0.10× input rate (read discount)
# Source: prompt-caching pricing in Anthropic's public API docs.


def test_cache_creation_billed_at_1_25x_input():
    # Sonnet 4.6 input rate = 300 cents per 1M tokens.
    # 1M cache_creation tokens × 1.25 = 375 cents.
    cost = cost_usd_cents(
        "claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=1_000_000,
    )
    assert cost == 375


def test_cache_read_billed_at_0_1x_input():
    # Sonnet 4.6 input rate = 300 cents per 1M tokens.
    # 1M cache_read tokens × 0.10 = 30 cents.
    cost = cost_usd_cents(
        "claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=1_000_000,
    )
    assert cost == 30


def test_cache_read_is_cheaper_than_standard_input():
    # Sanity: cache reads must be billed cheaper than standard input.
    # If we wired the multiplier the wrong way, this guards the regression.
    plain = cost_usd_cents("claude-sonnet-4-6", 1_000_000, 0)
    cached = cost_usd_cents(
        "claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=1_000_000,
    )
    assert cached < plain
    # And: cache write must be MORE expensive than plain input.
    cache_write = cost_usd_cents(
        "claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=1_000_000,
    )
    assert cache_write > plain


def test_pricing_with_all_four_token_types():
    # Sonnet 4.6: input 300/M, output 1500/M.
    # 1M input        × 300              = 300
    # 1M cache_create × 300 × 1.25       = 375
    # 1M cache_read   × 300 × 0.10       = 30
    # 1M output       × 1500             = 1500
    # ─────────────────────────────────────────
    # Total                              = 2205 cents
    cost = cost_usd_cents(
        "claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    assert cost == 2205


def test_cache_args_default_to_zero_for_backward_compat():
    # Existing callers passing only (model, input, output) must keep working.
    a = cost_usd_cents("claude-sonnet-4-6", 1_000_000, 1_000_000)
    b = cost_usd_cents(
        "claude-sonnet-4-6",
        1_000_000,
        1_000_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    assert a == b


def test_cache_pricing_scales_with_model_input_rate():
    # Opus input rate (1500/M) is 5× Sonnet (300/M), so cache costs scale 5×
    # at the same token count. This proves we use the model's own rate, not
    # a hardcoded number.
    sonnet_read = cost_usd_cents(
        "claude-sonnet-4-6",
        0,
        0,
        cache_read_input_tokens=1_000_000,
    )
    opus_read = cost_usd_cents(
        "claude-opus-4-7",
        0,
        0,
        cache_read_input_tokens=1_000_000,
    )
    assert opus_read == sonnet_read * 5
