"""Proxy router — the hot path.

POST /v1/messages:
  1. Verify tq_* token → load api_key row
  2. Pre-flight estimate worst-case cost (input+max_tokens at request rate)
  3. Atomically reserve worst-case in caps_today → 402 if it would bust cap
  4. Decrypt Anthropic key, forward request to providers/anthropic.py
  5. On cap cross mid-stream: inject synthetic message_stop
  6. Persist usage_event + reconcile (actual − reserved, may be negative)

C1 (atomic reservation) — replaces the prior read-decide-write sequence.
The old pattern read `spent_cents`, made a 402-or-pass decision, then wrote
spend after the upstream call. Under concurrency, N parallel requests would
all observe the same stale `spent_cents` and all pass — the cap was soft.
The new flow uses a single SQL `INSERT ... ON CONFLICT DO UPDATE WHERE` so
the check-and-increment is atomic. After the upstream call settles we
reconcile by adding `(actual − reserved)` (which can be negative — the
reservation always reflects worst case, so we usually refund).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

import bcrypt
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourniquet.billing.caps import add_spend, get_today_spend, is_over_cap, reserve_or_reject
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


async def _legacy_bcrypt_scan(raw: str, session: AsyncSession) -> ApiKey | None:
    """Fallback path for tokens minted before C3 (no `tq_token_sha256`).

    Loads only rows where `tq_token_sha256 IS NULL` and bcrypt-checks each.
    On match, populates `tq_token_sha256` so subsequent requests hit the
    SHA-256 fast path. This is a one-shot upgrade per legacy key — once
    the column is set, future requests never enter this scan.

    Returns None if no legacy row matches (caller raises 401).
    """
    result = await session.execute(
        select(ApiKey).where(ApiKey.tq_token_sha256.is_(None))
    )
    legacy_keys = result.scalars().all()

    for key in legacy_keys:
        if bcrypt.checkpw(raw.encode(), key.tq_token_hash.encode()):
            # Backfill so the next request short-circuits to the indexed path.
            key.tq_token_sha256 = hashlib.sha256(raw.encode()).hexdigest()
            await session.commit()
            return key

    return None


async def _resolve_api_key(token: str, session: AsyncSession) -> ApiKey:
    """Resolve a tq_* bearer token to its ApiKey row.

    Fast path: SHA-256(token) → unique-indexed lookup, single SELECT.
    Slow path: bcrypt scan over rows with NULL `tq_token_sha256` (legacy
    keys minted before C3). On match, the legacy key is upgraded so it
    never hits the slow path again.

    `tq_*` tokens are 32 bytes from `secrets.token_urlsafe(32)` (256 bits
    of entropy) — they are not user passwords, so bcrypt's slowness is
    unnecessary. SHA-256 + unique index gives O(1) auth.
    """
    raw = token.removeprefix("Bearer ").strip()

    sha = hashlib.sha256(raw.encode()).hexdigest()
    result = await session.execute(
        select(ApiKey).where(ApiKey.tq_token_sha256 == sha)
    )
    key = result.scalar_one_or_none()

    if key is None:
        # Legacy fallback: tokens minted before C3 have no sha256 column.
        key = await _legacy_bcrypt_scan(raw, session)

    if key is None:
        raise HTTPException(
            status_code=401,
            detail={"type": "invalid_token", "message": "Invalid or unknown Tourniquet token."},
        )

    return key


def _estimate_worst_case_cents(parsed_body: dict) -> tuple[str, int]:
    """Estimate the worst-case cost (in USD cents) of a request from its body.

    Worst case = (estimated input tokens from message char count, +25% pad)
    + max_tokens output. This is what we reserve up front so concurrent
    requests on the same key can't all squeak past a stale spent_cents
    read. Reconciliation refunds the over-estimate after the upstream
    response settles.
    """
    req_model = parsed_body.get("model", "claude-sonnet-4-6") or "claude-sonnet-4-6"
    req_max_tokens = int(parsed_body.get("max_tokens", 4096) or 4096)
    messages = parsed_body.get("messages", []) or []
    chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for block in c:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    chars += len(block["text"])
    # Char-count heuristic: ~4 chars per token, padded 25% for safety.
    est_input_tokens = max(1, int(chars / 4 * 1.25))
    return req_model, cost_usd_cents(req_model, est_input_tokens, req_max_tokens)


def _cap_hit_payload(
    *,
    cap_cents: int,
    spent_cents: int,
    today: date,
    lift_active: bool,
    lift_expires_at_iso: str | None,
) -> dict:
    """Build the canonical 402 `tourniquet_cap_hit` payload."""
    resets_at = (
        datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
        + timedelta(days=1)
    )
    currency = settings.display_currency
    return {
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
    }


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
        cap_cents = _effective_cap(api_key, now)
        lift_active = (
            api_key.lifted_cap_usd_cents is not None
            and api_key.lift_expires_at is not None
            and api_key.lift_expires_at > now
        )
        lift_expires_at_iso = api_key.lift_expires_at.isoformat() if lift_active else None

        # Single body parse — used for metadata, streaming detection, and worst-case cost.
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {}

        metadata_user_id = (parsed.get("metadata") or {}).get("user_id")
        if metadata_user_id is not None:
            metadata_user_id = str(metadata_user_id)[:255]

        is_streaming = bool(parsed.get("stream", False))
        user_agent = request.headers.get("user-agent", "")[:255]

        # ── C1: atomic reservation ────────────────────────────────────────────
        # The old pattern read spent_cents, decided 402-or-pass, then wrote
        # spend after the upstream call. Under bursty concurrency (Claude
        # Code firing 5–20 parallel tool calls on the same key) this was a
        # soft cap. Now we reserve the worst-case cost atomically, fail
        # fast if it would bust the cap, and reconcile after.
        reserved_cents = 0
        if api_key.kill_enabled:
            _model, reserved_cents = _estimate_worst_case_cents(parsed)
            ok = await reserve_or_reject(
                api_key.id, today, reserved_cents, cap_cents, session
            )
            if not ok:
                # Reservation rejected — read the current spend for the
                # response payload (best-effort; the actual gate already
                # ran in SQL).
                spent_cents = await get_today_spend(api_key.id, today, session)
                return JSONResponse(
                    status_code=402,
                    content=_cap_hit_payload(
                        cap_cents=cap_cents,
                        spent_cents=spent_cents,
                        today=today,
                        lift_active=lift_active,
                        lift_expires_at_iso=lift_expires_at_iso,
                    ),
                )
            # Commit the reservation NOW so concurrent requests see the
            # booked spend on their own atomic check.
            await session.commit()

        # `spent_cents` for cap-cross checks during the upstream stream — read
        # AFTER the reservation so it reflects this request's booking too.
        spent_cents_with_reservation = await get_today_spend(api_key.id, today, session)

        anthropic_key = _decrypt_anthropic_key(api_key.anthropic_key_encrypted)

        forward_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() in ("content-type", "anthropic-version", "anthropic-beta")
        }

        # ── Non-streaming path: forward, parse JSON usage, persist, reconcile ──
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

            actual_cost = cost_usd_cents(model_used or "claude-sonnet-4-6", in_tokens, out_tokens)
            # Reconcile: add (actual − reserved). Can be negative (refund of
            # over-estimate) or positive (under-estimate top-up — rare since
            # max_tokens caps the output).
            reconcile_delta = actual_cost - reserved_cents

            async with get_session() as write_session:
                event = UsageEvent(
                    api_key_id=api_key.id,
                    request_id=request_id or None,
                    model=model_used or "unknown",
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    cost_usd_cents=actual_cost,
                    cap_hit=False,
                    user_agent=user_agent or None,
                    metadata_user_id=metadata_user_id,
                )
                write_session.add(event)
                if reconcile_delta != 0:
                    await add_spend(api_key.id, today, reconcile_delta, write_session)
                # Threshold-alert wiring — fire 50%/80%/cap-hit alerts at most
                # once per day per key. Audit row is written to the same
                # session so it commits atomically with the spend.
                # Background fan_out task is spawned so the proxy response
                # isn't held up by Slack/Telegram round-trips.
                from tourniquet.alerts.notifier import maybe_fire_threshold_alert
                # Spend-after-this-request reflects the reconciled total.
                spend_after = (
                    spent_cents_with_reservation - reserved_cents + actual_cost
                )
                await maybe_fire_threshold_alert(
                    api_key,
                    spend_after,
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
        # The accumulator-based mid-stream cap-cross check still uses
        # `spent_cents_with_reservation` as the floor. Because we already
        # reserved worst-case, the reservation alone shouldn't trip this —
        # but if the reservation is somehow under (e.g. char heuristic
        # under-counts and actual usage exceeds reserved + remaining cap),
        # the kill switch still fires.

        async def _cap_check(acc: UsageAccumulator) -> bool:
            if not api_key.kill_enabled:
                return False
            c = cost_usd_cents(acc.model or "claude-sonnet-4-6", acc.input_tokens, acc.output_tokens)
            # spent_cents_with_reservation already counts THIS request's
            # worst-case reservation, so subtract it back out before adding
            # the actual cost so far.
            spent_other = spent_cents_with_reservation - reserved_cents
            return is_over_cap(spent_other + c, cap_cents)

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

            # Persist usage + reconcile after stream completes
            if accumulated:
                actual_cost = cost_usd_cents(
                    accumulated.model or "claude-sonnet-4-6",
                    accumulated.input_tokens,
                    accumulated.output_tokens,
                )
                reconcile_delta = actual_cost - reserved_cents
                async with get_session() as write_session:
                    event = UsageEvent(
                        api_key_id=api_key.id,
                        request_id=accumulated.request_id or None,
                        model=accumulated.model or "unknown",
                        input_tokens=accumulated.input_tokens,
                        output_tokens=accumulated.output_tokens,
                        cost_usd_cents=actual_cost,
                        cap_hit=cap_was_hit,
                        user_agent=user_agent or None,
                        metadata_user_id=metadata_user_id,
                    )
                    write_session.add(event)
                    if reconcile_delta != 0:
                        await add_spend(api_key.id, today, reconcile_delta, write_session)
                    # Threshold-alert wiring (streaming path) — same idempotency
                    # guarantees as the non-streaming path. Sees the spend
                    # *after* this stream's contribution.
                    from tourniquet.alerts.notifier import maybe_fire_threshold_alert
                    spend_after = (
                        spent_cents_with_reservation - reserved_cents + actual_cost
                    )
                    await maybe_fire_threshold_alert(
                        api_key,
                        spend_after,
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
