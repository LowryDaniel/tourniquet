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
    matching = [
        r for r in caplog.records if "claude-mystery-1" in r.getMessage()
    ]
    assert len(matching) == 1
