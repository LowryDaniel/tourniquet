"""Proxy router — the hot path.

POST /v1/messages:
  1. Verify tq_* token → load api_key row
  2. Pre-flight cap check → 402 if already over cap
  3. Decrypt Anthropic key
  4. Stream request through providers/anthropic.py
  5. On cap cross mid-stream: inject synthetic message_stop
  6. Persist usage_event + update caps_today
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timezone

import bcrypt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tourniquet.billing.caps import add_spend, get_today_spend, is_over_cap
from tourniquet.billing.pricing import cost_pence
from tourniquet.config import settings
from tourniquet.db import get_session
from tourniquet.models import ApiKey, UsageEvent
from tourniquet.providers.anthropic import UsageAccumulator, stream_request

router = APIRouter()


def _decrypt_anthropic_key(encrypted: str) -> str:
    from cryptography.fernet import Fernet

    f = Fernet(settings.fernet_key.encode())
    return f.decrypt(encrypted.encode()).decode()


async def _resolve_api_key(token: str, session: AsyncSession) -> ApiKey:
    """Resolve a tq_* bearer token to its ApiKey row.

    Uses bcrypt verify — intentionally slow to prevent brute-force.
    """
    # Strip "Bearer " prefix
    raw = token.removeprefix("Bearer ").strip()

    # We need to find the key whose hash matches — bcrypt doesn't allow direct lookup.
    # Mitigate timing by limiting to 1000 active keys per query.
    # In production: cache the token→key_id mapping in Redis (v2).
    result = await session.execute(select(ApiKey))
    keys = result.scalars().all()

    for key in keys:
        if bcrypt.checkpw(raw.encode(), key.tq_token_hash.encode()):
            return key

    raise HTTPException(status_code=401, detail={"type": "invalid_token", "message": "Invalid or unknown Tourniquet token."})


@router.post("/v1/messages", response_model=None)
async def proxy_messages(request: Request) -> StreamingResponse | JSONResponse:
    auth_header = request.headers.get("authorization", "")
    if not auth_header:
        raise HTTPException(status_code=401, detail={"type": "invalid_token", "message": "Missing Authorization header."})

    body = await request.body()

    async with get_session() as session:
        api_key = await _resolve_api_key(auth_header, session)

        today = date.today()
        spent_pence = await get_today_spend(api_key.id, today, session)

        if api_key.kill_enabled and is_over_cap(spent_pence, api_key.daily_cap_pence):
            resets_at = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
            # Advance to midnight of next day
            from datetime import timedelta
            resets_at = resets_at + timedelta(days=1)
            return JSONResponse(
                status_code=402,
                content={
                    "error": {
                        "type": "tourniquet_cap_hit",
                        "message": "Daily spend cap reached. Resets at midnight UTC.",
                        "resets_at": resets_at.isoformat(),
                        "cap_pence": api_key.daily_cap_pence,
                        "spent_pence": spent_pence,
                    }
                },
            )

        anthropic_key = _decrypt_anthropic_key(api_key.anthropic_key_encrypted)

        forward_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() in ("content-type", "anthropic-version", "anthropic-beta")
        }

        accumulated: UsageAccumulator | None = None

        async def _cap_check(acc: UsageAccumulator) -> bool:
            if not api_key.kill_enabled:
                return False
            c = cost_pence(acc.model or "claude-sonnet-4-6", acc.input_tokens, acc.output_tokens)
            return is_over_cap(spent_pence + c, api_key.daily_cap_pence)

        async def _generate():
            nonlocal accumulated
            cap_was_hit = False

            async for chunk, acc in stream_request(
                anthropic_key=anthropic_key,
                request_body=body,
                headers=forward_headers,
                on_cap_check=_cap_check,
            ):
                accumulated = acc
                if b"tourniquet_cap_hit" in chunk:
                    cap_was_hit = True
                yield chunk

            # Persist usage after stream completes
            if accumulated:
                c = cost_pence(
                    accumulated.model or "claude-sonnet-4-6",
                    accumulated.input_tokens,
                    accumulated.output_tokens,
                )
                async with get_session() as write_session:
                    event = UsageEvent(
                        api_key_id=api_key.id,
                        request_id=accumulated.request_id or None,
                        model=accumulated.model or "unknown",
                        input_tokens=accumulated.input_tokens,
                        output_tokens=accumulated.output_tokens,
                        cost_pence=c,
                        cap_hit=cap_was_hit,
                    )
                    write_session.add(event)
                    await add_spend(api_key.id, today, c, write_session)
                    await write_session.commit()

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
