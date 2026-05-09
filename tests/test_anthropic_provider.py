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
    CAP_HIT_HEADER,
    UsageAccumulator,
    build_cap_hit_event,
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


@pytest.mark.asyncio
async def test_sse_parser_handles_data_without_preceding_event():
    """A bare `data:` line with no preceding `event:` must not raise NameError
    and must not be ingested into the accumulator (M3)."""
    sse_response = (
        # Bare data line first — no preceding `event:`. The parser must skip ingest.
        'data: {"type":"message_start","message":{"id":"msg_orphan","model":"claude-sonnet-4-6","usage":{"input_tokens":999}}}\n\n'
        # Then a normal event so the rest of the stream is well-formed.
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_real","model":"claude-sonnet-4-6","usage":{"input_tokens":50}}}\n\n'
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

        # Critical: no NameError raised, stream completes.
        assert final_acc is not None
        # The orphan `data:` line must NOT have been ingested. Only the real event counts.
        assert final_acc.input_tokens == 50
        assert final_acc.request_id == "msg_real"


@pytest.mark.asyncio
async def test_sse_parser_resets_event_type_on_blank_line():
    """A blank line is the SSE event terminator. The parser must reset
    event_type so subsequent stray `data:` lines aren't tagged with the
    previous event's type (M3)."""
    sse_response = (
        # First event — message_start with input_tokens=10
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_one","model":"claude-sonnet-4-6","usage":{"input_tokens":10}}}\n'
        '\n'
        # Blank line separates events; event_type should be reset here.
        # An orphan `data:` line should be skipped (not re-ingested as message_start).
        'data: {"type":"message_start","message":{"id":"msg_orphan","model":"claude-sonnet-4-6","usage":{"input_tokens":7777}}}\n\n'
        # Second proper event — message_delta with output_tokens=5
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n'
    )

    async def never_hit(_acc):
        return False

    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, text=sse_response, headers={"content-type": "text/event-stream"})
        )

        final_acc = None
        async for _chunk, acc in stream_request(
            anthropic_key="sk-ant-test",
            request_body=b'{"model":"claude-sonnet-4-6","messages":[]}',
            headers={"content-type": "application/json"},
            on_cap_check=never_hit,
        ):
            final_acc = acc

        assert final_acc is not None
        # Confirms: the blank line reset event_type. The orphan `data:` (input_tokens=7777)
        # was NOT ingested as a message_start (would have overwritten input_tokens to 7777).
        # And the message_delta WAS correctly tagged (output_tokens=5, stop_reason=end_turn).
        assert final_acc.input_tokens == 10
        assert final_acc.request_id == "msg_one"
        assert final_acc.output_tokens == 5
        assert final_acc.stop_reason == "end_turn"


def test_cap_hit_event_uses_documented_stop_reason():
    """M2: synthetic message_stop carries `stop_reason: end_turn`, not the
    legacy `tourniquet_cap_hit` string that strict SDKs reject."""
    # Default event (no cap/spent context)
    assert '"stop_reason":"end_turn"' in CAP_HIT_EVENT
    # The legacy unknown-enum value must not appear in the message_stop block.
    msg_stop_block = CAP_HIT_EVENT.split("event: error", 1)[0]
    assert "tourniquet_cap_hit" not in msg_stop_block


def test_cap_hit_event_emits_separate_error_block():
    """M2: a separate `event: error` SSE block carries the tourniquet_cap_hit JSON."""
    event = build_cap_hit_event(
        cap_usd_cents=500,
        spent_usd_cents=512,
        resets_at="2026-05-10T00:00:00+00:00",
    )
    assert "event: error\n" in event
    # Pull out the data line of the error block and parse it.
    error_block = event.split("event: error\n", 1)[1]
    error_data_line = error_block.splitlines()[0]
    assert error_data_line.startswith("data: ")
    payload = json.loads(error_data_line[len("data: "):])
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "tourniquet_cap_hit"
    assert payload["error"]["cap_usd_cents"] == 500
    assert payload["error"]["spent_usd_cents"] == 512
    assert payload["error"]["resets_at"] == "2026-05-10T00:00:00+00:00"


def test_cap_hit_header_constant_is_documented():
    """The `X-Tourniquet-Cap-Hit` header is the non-streaming-client signal."""
    assert CAP_HIT_HEADER == "X-Tourniquet-Cap-Hit"
