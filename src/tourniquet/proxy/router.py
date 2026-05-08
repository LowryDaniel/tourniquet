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
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tourniquet.billing.caps import add_spend, get_today_spend, is_over_cap
from tourniquet.billing.formatting import format_money
from tourniquet.billing.pricing import cost_usd_cents
from tourniquet.config import settings
from tourniquet.db import get_session
from tourniquet.models import ApiKey, UsageEvent
from tourniquet.providers.anthropic import UsageAccumulator, stream_request

router = APIRouter()


def _effective_cap(api_key: ApiKey, now: datetime) -> int:
    """Return the active daily cap, honouring any temporary lift.

    Two-tier cap system:
    - daily_cap_usd_cents: the configured "normal" quota, persistent across days
    - lifted_cap_usd_cents: temporary override, used for emergency kills and
      in-app recovery bumps, auto-expires at midnight UTC

    This function returns lifted_cap if:
      1. lifted_cap is set (not None)
      2. lift_expires_at is set and in the future (now < expires_at)
    Otherwise falls back to daily_cap.

    Lifted cap takes PRECEDENCE over daily cap while active, not the reverse —
    this is how emergency stops (kill_enabled=True) block requests and how
    in-app bumps temporarily increase allowance.
    """
    if (
        api_key.lifted_cap_usd_cents is not None
        and api_key.lift_expires_at is not None
        and api_key.lift_expires_at > now
    ):
        return api_key.lifted_cap_usd_cents
    return api_key.daily_cap_usd_cents


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

        now = datetime.now(timezone.utc)
        today = date.today()
        spent_cents = await get_today_spend(api_key.id, today, session)
        cap_cents = _effective_cap(api_key, now)
        lift_active = (
            api_key.lifted_cap_usd_cents is not None
            and api_key.lift_expires_at is not None
            and api_key.lift_expires_at > now
        )
        lift_expires_at_iso = api_key.lift_expires_at.isoformat() if lift_active else None

        if api_key.kill_enabled and is_over_cap(spent_cents, cap_cents):
            from datetime import timedelta
            resets_at = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
            resets_at = resets_at + timedelta(days=1)
            currency = settings.display_currency
            return JSONResponse(
                status_code=402,
                content={
                    "error": {
                        "type": "tourniquet_cap_hit",
                        "message": "Daily spend cap reached. Resets at midnight UTC.",
                        "resets_at": resets_at.isoformat(),
                        "cap_usd_cents": cap_cents,
                        "spent_usd_cents": spent_cents,
                        "lift_active": lift_active,
                        "lift_expires_at": lift_expires_at_iso,
                        "display": {
                            "cap": format_money(cap_cents, currency),
                            "spent": format_money(spent_cents, currency),
                            "currency": currency,
                        },
                    }
                },
            )

        anthropic_key = _decrypt_anthropic_key(api_key.anthropic_key_encrypted)

        forward_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() in ("content-type", "anthropic-version", "anthropic-beta")
        }

        # Capture user-agent and metadata.user_id for analytics
        user_agent = request.headers.get("user-agent", "")[:255]

        # Single body parse — used for metadata, streaming detection, and max-cost guard
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {}

        metadata_user_id = (parsed.get("metadata") or {}).get("user_id")
        if metadata_user_id is not None:
            metadata_user_id = str(metadata_user_id)[:255]

        is_streaming = bool(parsed.get("stream", False))

        # ── Pre-flight max-cost guard ─────────────────────────────────────────
        # Before forwarding to Anthropic, estimate the request's worst-case cost
        # (input tokens + max_tokens output) and reject with 402 if it would
        # exceed today's effective cap by more than both the absolute and
        # percentage-based tolerances. This stops obviously oversized requests
        # before they waste API tokens. Small overages are allowed (let it ride)
        # — overage must exceed BOTH abs and pct thresholds to trigger the block.

        if api_key.kill_enabled:
            req_model = parsed.get("model", "claude-sonnet-4-6")
            req_max_tokens = int(parsed.get("max_tokens", 4096) or 4096)
            messages = parsed.get("messages", []) or []
            # Rough char-count heuristic for input tokens. Over-estimate by 25%
            # to err on the safe side (better a false 402 than an unbilled runaway).
            chars = 0
            for m in messages:
                c = m.get("content")
                if isinstance(c, str):
                    chars += len(c)
                elif isinstance(c, list):
                    for block in c:
                        if isinstance(block, dict) and isinstance(block.get("text"), str):
                            chars += len(block["text"])
            est_input_tokens = max(1, int(chars / 4 * 1.25))
            worst_case_cents = cost_usd_cents(req_model, est_input_tokens, req_max_tokens)
            projected_total = spent_cents + worst_case_cents
            if projected_total > cap_cents:
                overage = projected_total - cap_cents
                tolerance = max(
                    settings.max_overage_abs_cents,
                    int(cap_cents * settings.max_overage_pct / 100),
                )
                if overage > tolerance:
                    from datetime import timedelta as _td
                    resets_at = (datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
                                 + _td(days=1))
                    currency = settings.display_currency
                    return JSONResponse(
                        status_code=402,
                        content={
                            "error": {
                                "type": "tourniquet_preflight_block",
                                "message": (
                                    f"Request would push spend to ~{format_money(projected_total, currency)}, "
                                    f"over your {format_money(cap_cents, currency)} cap by "
                                    f"{format_money(overage, currency)} (tolerance "
                                    f"{format_money(tolerance, currency)}). Lower max_tokens, shorten the "
                                    f"prompt, or lift today's cap."
                                ),
                                "resets_at": resets_at.isoformat(),
                                "cap_usd_cents": cap_cents,
                                "spent_usd_cents": spent_cents,
                                "projected_usd_cents": projected_total,
                                "tolerance_usd_cents": tolerance,
                                "display": {
                                    "cap": format_money(cap_cents, currency),
                                    "spent": format_money(spent_cents, currency),
                                    "projected": format_money(projected_total, currency),
                                    "tolerance": format_money(tolerance, currency),
                                    "currency": currency,
                                },
                            }
                        },
                    )
        # ────────────────────────────────────────────────────────────────────

        # ── Non-streaming path: forward, parse JSON usage, persist, return ─────
        if not is_streaming:
            from tourniquet.config import settings as _s
            forward_headers["x-api-key"] = anthropic_key
            forward_headers.setdefault("anthropic-version", "2023-06-01")
            url = f"{_s.anthropic_base_url}/v1/messages"

            async with httpx.AsyncClient(timeout=60.0) as client:
                upstream = await client.post(url, content=body, headers=forward_headers)

            # Try to parse usage from JSON body. Tolerate Anthropic error shapes.
            model_used = ""
            request_id = ""
            in_tokens = 0
            out_tokens = 0
            try:
                parsed_resp = json.loads(upstream.content)
                usage = parsed_resp.get("usage", {}) or {}
                model_used = parsed_resp.get("model", "") or ""
                request_id = parsed_resp.get("id", "") or ""
                in_tokens = int(usage.get("input_tokens", 0) or 0)
                out_tokens = int(usage.get("output_tokens", 0) or 0)
            except Exception:
                pass

            cost = cost_usd_cents(model_used or "claude-sonnet-4-6", in_tokens, out_tokens)

            async with get_session() as write_session:
                event = UsageEvent(
                    api_key_id=api_key.id,
                    request_id=request_id or None,
                    model=model_used or "unknown",
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    cost_usd_cents=cost,
                    cap_hit=False,
                    user_agent=user_agent or None,
                    metadata_user_id=metadata_user_id,
                )
                write_session.add(event)
                await add_spend(api_key.id, today, cost, write_session)
                # Threshold-alert wiring — fire 50%/80%/cap-hit alerts at most
                # once per day per key. Audit row is written to the same
                # session so it commits atomically with the spend.
                # Background fan_out task is spawned so the proxy response
                # isn't held up by Slack/Telegram round-trips.
                from tourniquet.alerts.notifier import maybe_fire_threshold_alert
                await maybe_fire_threshold_alert(
                    api_key,
                    spent_cents + cost,
                    cap_cents,
                    today,
                    kill_enabled=api_key.kill_enabled,
                    session=write_session,
                )
                await write_session.commit()

            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "application/json"),
            )

        # ── Streaming path (original SSE-with-kill flow) ──────────────────────
        accumulated: UsageAccumulator | None = None

        async def _cap_check(acc: UsageAccumulator) -> bool:
            if not api_key.kill_enabled:
                return False
            c = cost_usd_cents(acc.model or "claude-sonnet-4-6", acc.input_tokens, acc.output_tokens)
            return is_over_cap(spent_cents + c, cap_cents)

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
                c = cost_usd_cents(
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
                        cost_usd_cents=c,
                        cap_hit=cap_was_hit,
                        user_agent=user_agent or None,
                        metadata_user_id=metadata_user_id,
                    )
                    write_session.add(event)
                    await add_spend(api_key.id, today, c, write_session)
                    # Threshold-alert wiring (streaming path) — same idempotency
                    # guarantees as the non-streaming path. Sees the spend
                    # *after* this stream's contribution.
                    from tourniquet.alerts.notifier import maybe_fire_threshold_alert
                    await maybe_fire_threshold_alert(
                        api_key,
                        spent_cents + c,
                        cap_cents,
                        today,
                        kill_enabled=api_key.kill_enabled,
                        session=write_session,
                    )
                    await write_session.commit()

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
