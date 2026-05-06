"""Pricing calculations — pence integer, no float drift."""

from tourniquet.billing.pricing import cost_pence


def test_cost_pence_zero_tokens():
    assert cost_pence("claude-sonnet-4-6", 0, 0) == 0


def test_cost_pence_known_model():
    # Sonnet 4.6: 237p/1M input, 1185p/1M output
    # 1M input + 0 output = 237p, rounded up
    assert cost_pence("claude-sonnet-4-6", 1_000_000, 0) == 237
    # 0 input + 1M output = 1185p
    assert cost_pence("claude-sonnet-4-6", 0, 1_000_000) == 1185


def test_cost_pence_unknown_model_falls_back():
    # Unknown model uses Sonnet rates
    assert cost_pence("future-claude-9000", 1_000_000, 0) == 237


def test_cost_pence_rounds_up():
    # Tiny request: 1 input + 1 output ≈ 0.000001422 pence → rounds up to 1
    result = cost_pence("claude-sonnet-4-6", 1, 1)
    assert result >= 1
    assert isinstance(result, int)


def test_cost_pence_realistic_request():
    # 500 input + 200 output on Sonnet
    # = (500 * 237 + 200 * 1185) / 1M = (118_500 + 237_000) / 1M = 0.355p
    # Rounds up to 1p
    cost = cost_pence("claude-sonnet-4-6", 500, 200)
    assert cost == 1


def test_cost_pence_opus_more_expensive():
    sonnet = cost_pence("claude-sonnet-4-6", 1_000_000, 1_000_000)
    opus = cost_pence("claude-opus-4-7", 1_000_000, 1_000_000)
    assert opus > sonnet
