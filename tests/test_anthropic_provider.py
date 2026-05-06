"""Anthropic provider — the streaming kill mechanic.

The most load-bearing test in the codebase: when the cap-check callback
returns True mid-stream, the provider yields the synthetic message_stop
event and stops forwarding upstream bytes.
"""

import json

import httpx
import pytest
import respx

from tourniquet.providers.anthropic import (
    CAP_HIT_EVENT,
    UsageAccumulator,
    stream_request,
)


def test_usage_accumulator_ingests_message_start():
    acc = UsageAccumulator()
    acc.ingest_event("message_start", {
        "message": {
            "id": "msg_123",
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 100},
        }
    })
    assert acc.input_tokens == 100
    assert acc.model == "claude-sonnet-4-6"
    assert acc.request_id == "msg_123"


def test_usage_accumulator_ingests_message_delta():
    acc = UsageAccumulator()
    acc.ingest_event("message_delta", {
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 42},
    })
    assert acc.output_tokens == 42
    assert acc.stop_reason == "end_turn"


def test_usage_accumulator_takes_final_output_count():
    """message_delta usage is cumulative; we keep the last seen value."""
    acc = UsageAccumulator()
    acc.ingest_event("message_delta", {"delta": {}, "usage": {"output_tokens": 10}})
    acc.ingest_event("message_delta", {"delta": {}, "usage": {"output_tokens": 25}})
    assert acc.output_tokens == 25


@pytest.mark.asyncio
async def test_stream_passes_through_under_cap():
    """When cap-check always returns False, all bytes flow through."""
    sse_response = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_abc","model":"claude-sonnet-4-6","usage":{"input_tokens":50}}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )

    async def never_hit(_acc):
        return False

    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, text=sse_response, headers={"content-type": "text/event-stream"})
        )

        chunks = []
        final_acc = None
        async for chunk, acc in stream_request(
            anthropic_key="sk-ant-test",
            request_body=b'{"model":"claude-sonnet-4-6","messages":[]}',
            headers={"content-type": "application/json"},
            on_cap_check=never_hit,
        ):
            chunks.append(chunk)
            final_acc = acc

        body = b"".join(chunks).decode()
        assert "message_start" in body
        assert "Hello" in body
        assert "message_stop" in body
        assert "tourniquet_cap_hit" not in body
        assert final_acc is not None
        assert final_acc.input_tokens == 50
        assert final_acc.output_tokens == 5
        assert final_acc.model == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_stream_injects_cap_hit_event_mid_stream():
    """When cap-check returns True, synthetic message_stop is emitted and stream terminates."""
    sse_response = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_xyz","model":"claude-sonnet-4-6","usage":{"input_tokens":1000000}}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"This should NOT appear"}}\n\n'
    )

    async def hit_after_message_start(acc):
        # Trigger cap-hit as soon as input tokens are visible
        return acc.input_tokens > 0

    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, text=sse_response, headers={"content-type": "text/event-stream"})
        )

        chunks = []
        async for chunk, _acc in stream_request(
            anthropic_key="sk-ant-test",
            request_body=b'{"model":"claude-sonnet-4-6","messages":[]}',
            headers={"content-type": "application/json"},
            on_cap_check=hit_after_message_start,
        ):
            chunks.append(chunk)

        body = b"".join(chunks).decode()
        assert "tourniquet_cap_hit" in body
        assert "This should NOT appear" not in body
