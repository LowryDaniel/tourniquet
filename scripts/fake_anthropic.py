"""Fake Anthropic SSE upstream for E2E testing.

Pretends to be api.anthropic.com — accepts POST /v1/messages and emits a
canned SSE stream with a configurable token count so we can verify
Tourniquet's cap accounting end-to-end.

Configure how big the response is via env:
    FAKE_INPUT_TOKENS=1000 FAKE_OUTPUT_TOKENS=500 python scripts/fake_anthropic.py

Listens on port 9999 by default.
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()

INPUT_TOKENS = int(os.environ.get("FAKE_INPUT_TOKENS", "1000"))
OUTPUT_TOKENS = int(os.environ.get("FAKE_OUTPUT_TOKENS", "500"))


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@app.post("/v1/messages")
async def messages():
    async def gen():
        yield _sse("message_start", {
            "type": "message_start",
            "message": {
                "id": "msg_fake_001",
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": INPUT_TOKENS},
            },
        })
        yield _sse("content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
        yield _sse("content_block_delta", {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello from fake upstream."},
        })
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield _sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": OUTPUT_TOKENS},
        })
        yield _sse("message_stop", {"type": "message_stop"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/health")
async def health():
    return {"status": "ok", "role": "fake-upstream"}
