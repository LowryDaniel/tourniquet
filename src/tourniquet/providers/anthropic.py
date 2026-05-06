"""Anthropic provider — streaming proxy and token counting.

Forwards requests to api.anthropic.com verbatim.
Reads usage from SSE events (no tiktoken, no counting endpoint).

SSE event sequence:
  message_start  → usage.input_tokens  (fixed at request start)
  content_block_start
  content_block_delta (N)
  content_block_stop
  message_delta  → usage.output_tokens (cumulative, read final value)
  message_stop
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import httpx

from tourniquet.config import settings


@dataclass
class UsageAccumulator:
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    model: str = ""
    request_id: str = ""
    _events: list[dict[str, Any]] = field(default_factory=list)

    def ingest_event(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == "message_start":
            msg = data.get("message", {})
            self.model = msg.get("model", "")
            self.request_id = msg.get("id", "")
            usage = msg.get("usage", {})
            self.input_tokens = usage.get("input_tokens", 0)
        elif event_type == "message_delta":
            usage = data.get("usage", {})
            self.output_tokens = usage.get("output_tokens", self.output_tokens)
            self.stop_reason = data.get("delta", {}).get("stop_reason", self.stop_reason)


CAP_HIT_EVENT = (
    'event: message_stop\n'
    'data: {"type":"message_stop","stop_reason":"tourniquet_cap_hit"}\n\n'
)


async def stream_request(
    *,
    anthropic_key: str,
    request_body: bytes,
    headers: dict[str, str],
    on_cap_check: Any,  # callable(accumulator) -> bool: True = cap hit, terminate
) -> AsyncGenerator[tuple[bytes, UsageAccumulator], None]:
    """Stream a request to Anthropic, yielding (chunk, accumulator) pairs.

    Caller is responsible for cap checking via on_cap_check callback.
    Yields the cap-hit synthetic event and stops when cap is hit.
    """
    acc = UsageAccumulator()

    forward_headers = {
        k: v for k, v in headers.items()
        if k.lower() in ("content-type", "anthropic-version", "anthropic-beta")
    }
    forward_headers["x-api-key"] = anthropic_key
    forward_headers.setdefault("anthropic-version", "2023-06-01")

    url = f"{settings.anthropic_base_url}/v1/messages"

    async with httpx.AsyncClient(timeout=30.0) as client:
        async with client.stream("POST", url, content=request_body, headers=forward_headers) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event_type = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    raw = line[len("data:"):].strip()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        data = {}
                    acc.ingest_event(event_type, data)

                    if await on_cap_check(acc):
                        yield CAP_HIT_EVENT.encode(), acc
                        return

                yield (line + "\n").encode(), acc
            yield b"\n", acc
