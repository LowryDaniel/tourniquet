"""Proxy integration tests.

Three critical scenarios:
1. Request under cap → proxied cleanly, usage persisted
2. Request that crosses cap mid-stream → synthetic message_stop injected, connection closed
3. Request on a different key (multi-key isolation) → cap from the correct key used
"""

# Tests are stubs — implementations added during W1 build.

import json

import httpx
import pytest
import respx

from tourniquet.providers.anthropic import stream_request


@pytest.mark.asyncio
async def test_health(client):  # type: ignore[no-untyped-def]
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_proxy_under_cap():
    """Proxied request under cap returns Anthropic response unchanged."""
    pytest.skip("implement in W1 with respx mock")


@pytest.mark.asyncio
async def test_proxy_cap_hit_mid_stream():
    """Request that crosses cap mid-stream receives synthetic message_stop."""
    pytest.skip("implement in W1 with respx mock")


@pytest.mark.asyncio
async def test_multi_key_isolation():
    """Key A's spend does not affect Key B's cap."""
    pytest.skip("implement in W1")


# ─────────────────────────────────────────────────────────────────────────────
# M2: streaming cap-hit signal shape — see docs/code-review-remediation.md.
# These tests exercise the streaming path end-to-end via the provider so the
# emitted bytes match what a downstream Anthropic SDK would actually parse.
# ─────────────────────────────────────────────────────────────────────────────


def _split_sse_blocks(body: str) -> list[dict[str, str]]:
    """Parse an SSE byte-stream body into a list of {event, data} dicts."""
    blocks: list[dict[str, str]] = []
    for raw_block in body.split("\n\n"):
        block = raw_block.strip()
        if not block:
            continue
        ev: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith("event:"):
                ev["event"] = line[len("event:"):].strip()
            elif line.startswith("data:"):
                ev["data"] = line[len("data:"):].strip()
        if ev:
            blocks.append(ev)
    return blocks


@pytest.mark.asyncio
async def test_streaming_cap_hit_uses_documented_stop_reason():
    """M2: when the cap is hit mid-stream, the synthetic message_stop carries
    `stop_reason: end_turn` (a documented Anthropic enum) so strict SDKs do
    not reject the event.
    """
    sse_response = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_cap","model":"claude-sonnet-4-6","usage":{"input_tokens":1000000}}}\n\n'
    )

    async def hit_immediately(acc):
        return acc.input_tokens > 0

    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, text=sse_response, headers={"content-type": "text/event-stream"})
        )

        chunks = []
        async for chunk, _acc in stream_request(
            anthropic_key="sk-ant-test",
            request_body=b'{"model":"claude-sonnet-4-6","messages":[],"stream":true}',
            headers={"content-type": "application/json"},
            on_cap_check=hit_immediately,
        ):
            chunks.append(chunk)

        body = b"".join(chunks).decode()
        blocks = _split_sse_blocks(body)

        # Find the synthetic message_stop block.
        stop_blocks = [b for b in blocks if b.get("event") == "message_stop"]
        assert stop_blocks, f"no message_stop block in body:\n{body}"
        # The synthetic stop block — last one — must use documented enum.
        synthetic = stop_blocks[-1]
        payload = json.loads(synthetic["data"])
        assert payload["stop_reason"] == "end_turn", (
            f"expected stop_reason=end_turn, got {payload}"
        )
        # And the legacy unknown-enum value must not appear in the stop block.
        assert "tourniquet_cap_hit" not in synthetic["data"]


@pytest.mark.asyncio
async def test_streaming_cap_hit_emits_tourniquet_error_event():
    """M2: cap-hit signaling rides on a separate `event: error` SSE block
    that carries the documented tourniquet_cap_hit JSON. SDKs that surface
    unknown SSE events expose this; clients that ignore unknown events still
    get a clean end_turn from the message_stop block.
    """
    sse_response = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_cap","model":"claude-sonnet-4-6","usage":{"input_tokens":1000000}}}\n\n'
    )

    async def hit_immediately(acc):
        return acc.input_tokens > 0

    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, text=sse_response, headers={"content-type": "text/event-stream"})
        )

        chunks = []
        async for chunk, _acc in stream_request(
            anthropic_key="sk-ant-test",
            request_body=b'{"model":"claude-sonnet-4-6","messages":[],"stream":true}',
            headers={"content-type": "application/json"},
            on_cap_check=hit_immediately,
        ):
            chunks.append(chunk)

        body = b"".join(chunks).decode()
        blocks = _split_sse_blocks(body)

        error_blocks = [b for b in blocks if b.get("event") == "error"]
        assert error_blocks, f"no `event: error` block in body:\n{body}"
        err_payload = json.loads(error_blocks[-1]["data"])
        assert err_payload["type"] == "error"
        assert err_payload["error"]["type"] == "tourniquet_cap_hit"
        # Schema sanity — the documented fields are present.
        assert "cap_usd_cents" in err_payload["error"]
        assert "spent_usd_cents" in err_payload["error"]
        assert "resets_at" in err_payload["error"]
