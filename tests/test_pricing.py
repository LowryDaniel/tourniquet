"""Pricing calculations — USD cents integer, no float drift."""

from tourniquet.billing.pricing import cost_usd_cents


def test_zero_tokens():
    assert cost_usd_cents("claude-sonnet-4-6", 0, 0) == 0


def test_sonnet_1m_input():
    # Sonnet 4.6: $3/1M input = 300 cents
    assert cost_usd_cents("claude-sonnet-4-6", 1_000_000, 0) == 300


def test_sonnet_1m_output():
    # Sonnet 4.6: $15/1M output = 1500 cents
    assert cost_usd_cents("claude-sonnet-4-6", 0, 1_000_000) == 1500


def test_unknown_model_falls_back_to_sonnet():
    # Unknown model uses Sonnet rates
    assert cost_usd_cents("future-claude-9000", 1_000_000, 0) == 300


def test_rounds_up():
    # Tiny request: 1 input + 1 output — well below 1 cent, rounds up to 1
    result = cost_usd_cents("claude-sonnet-4-6", 1, 1)
    assert result >= 1
    assert isinstance(result, int)


def test_opus_more_expensive_than_sonnet():
    sonnet = cost_usd_cents("claude-sonnet-4-6", 1_000_000, 1_000_000)
    opus = cost_usd_cents("claude-opus-4-7", 1_000_000, 1_000_000)
    assert opus > sonnet
