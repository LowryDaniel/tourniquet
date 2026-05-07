"""Admin routes — cap lift management.

Endpoints:
  POST /admin/lift   — temporarily raise a key's daily cap
  POST /admin/unlift — clear a lift early

Auth: Bearer tq_* token in Authorization header — must match the key being lifted.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Literal

import bcrypt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourniquet.db import get_session
from tourniquet.models import ApiKey

router = APIRouter(prefix="/admin")


# ── Pydantic models ────────────────────────────────────────────────────────────

class LiftRequest(BaseModel):
    key_id: str = Field(..., description="UUID or key name")
    mode: Literal["multiplier", "to", "to_ceiling"] = "multiplier"
    multiplier: float = Field(2.0, gt=0)
    to_amount_usd_cents: int | None = None
    duration_mode: Literal["until_midnight_utc", "for_hours", "to_time"] = "until_midnight_utc"
    duration_hours: float | None = None
    duration_to_time: str | None = Field(None, description="HH:MM, interpreted as today or tomorrow if past")


class LiftResponse(BaseModel):
    key_id: str
    key_name: str
    previous_cap_usd_cents: int
    lifted_cap_usd_cents: int
    lift_expires_at: str
    ceiling_clamped: bool
    absolute_ceiling_usd_cents: int


class UnliftResponse(BaseModel):
    key_id: str
    key_name: str
    restored_cap_usd_cents: int


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _resolve_and_auth(token: str, key_identifier: str, session: AsyncSession) -> ApiKey:
    """Resolve token to ApiKey and verify it matches the requested key_identifier."""
    raw = token.removeprefix("Bearer ").strip()

    # Load all keys — same approach as proxy (Redis cache in v2)
    result = await session.execute(select(ApiKey))
    keys = result.scalars().all()

    # Find the key matching the identifier (UUID prefix or exact name)
    target: ApiKey | None = None
    for k in keys:
        name_match = k.name == key_identifier
        uuid_match = str(k.id).startswith(key_identifier) and len(key_identifier) >= 8
        if name_match or uuid_match:
            target = k
            break

    if target is None:
        raise HTTPException(status_code=404, detail="Key not found")

    # Verify the bearer token belongs to this key
    if not bcrypt.checkpw(raw.encode(), target.tq_token_hash.encode()):
        raise HTTPException(status_code=401, detail="Token does not match the requested key")

    return target


def _compute_expiry(
    duration_mode: str,
    duration_hours: float | None,
    duration_to_time: str | None,
    now: datetime,
) -> datetime:
    """Compute lift expiry from duration params."""
    if duration_mode == "until_midnight_utc":
        # Next midnight UTC — users in other timezones see their lift expire at whatever
        # local time corresponds to UTC midnight. This is intentional: the daily spend
        # resets at midnight UTC, so the lift is coterminous with the spend period.
        tomorrow = (now.date() + timedelta(days=1))
        return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)

    if duration_mode == "for_hours":
        if not duration_hours or duration_hours <= 0:
            raise HTTPException(status_code=422, detail="duration_hours must be > 0 when duration_mode='for_hours'")
        return now + timedelta(hours=duration_hours)

    if duration_mode == "to_time":
        if not duration_to_time:
            raise HTTPException(status_code=422, detail="duration_to_time required when duration_mode='to_time'")
        m = re.match(r"^(\d{1,2}):(\d{2})$", duration_to_time)
        if not m:
            raise HTTPException(status_code=422, detail="duration_to_time must be HH:MM")
        hh, mm = int(m.group(1)), int(m.group(2))
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    raise HTTPException(status_code=422, detail=f"Unknown duration_mode: {duration_mode}")


def _compute_lift_cap(
    mode: str,
    base_cap: int,
    absolute_ceiling: int,
    multiplier: float,
    to_amount: int | None,
) -> tuple[int, bool]:
    """Return (lifted_cap_cents, ceiling_clamped)."""
    if mode == "multiplier":
        raw = int(base_cap * multiplier)
    elif mode == "to":
        if to_amount is None:
            raise HTTPException(status_code=422, detail="to_amount_usd_cents required when mode='to'")
        raw = to_amount
    elif mode == "to_ceiling":
        raw = absolute_ceiling
    else:
        raise HTTPException(status_code=422, detail=f"Unknown mode: {mode}")

    clamped = raw > absolute_ceiling
    return min(raw, absolute_ceiling), clamped


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/lift", response_model=LiftResponse)
async def lift_cap(request: Request, payload: LiftRequest) -> LiftResponse:
    """Lift the daily cap for a key temporarily.

    Auth: Bearer tq_* token in Authorization header — must match the key being lifted.
    The lift is clamped to absolute_ceiling_usd_cents under all conditions.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    now = datetime.now(timezone.utc)

    async with get_session() as session:
        api_key = await _resolve_and_auth(auth_header, payload.key_id, session)

        lifted_cents, clamped = _compute_lift_cap(
            mode=payload.mode,
            base_cap=api_key.daily_cap_usd_cents,
            absolute_ceiling=api_key.absolute_ceiling_usd_cents,
            multiplier=payload.multiplier,
            to_amount=payload.to_amount_usd_cents,
        )

        expires_at = _compute_expiry(
            duration_mode=payload.duration_mode,
            duration_hours=payload.duration_hours,
            duration_to_time=payload.duration_to_time,
            now=now,
        )

        db_key = await session.get(ApiKey, api_key.id)
        db_key.lifted_cap_usd_cents = lifted_cents
        db_key.lift_expires_at = expires_at
        await session.commit()

        return LiftResponse(
            key_id=str(api_key.id),
            key_name=api_key.name,
            previous_cap_usd_cents=api_key.daily_cap_usd_cents,
            lifted_cap_usd_cents=lifted_cents,
            lift_expires_at=expires_at.isoformat(),
            ceiling_clamped=clamped,
            absolute_ceiling_usd_cents=api_key.absolute_ceiling_usd_cents,
        )


@router.post("/unlift", response_model=UnliftResponse)
async def unlift_cap(request: Request, payload: LiftRequest) -> UnliftResponse:
    """Clear a cap lift early, restoring the original daily cap immediately.

    Accepts the same key_id payload as /lift; other fields are ignored.
    Auth: same Bearer tq_* token requirement.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    async with get_session() as session:
        api_key = await _resolve_and_auth(auth_header, payload.key_id, session)

        db_key = await session.get(ApiKey, api_key.id)
        db_key.lifted_cap_usd_cents = None
        db_key.lift_expires_at = None
        await session.commit()

        return UnliftResponse(
            key_id=str(api_key.id),
            key_name=api_key.name,
            restored_cap_usd_cents=api_key.daily_cap_usd_cents,
        )
