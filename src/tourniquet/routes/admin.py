"""Admin routes — cap lift management and kill-now magic links.

Endpoints:
  POST /admin/lift          — temporarily raise a key's daily cap
  POST /admin/unlift        — clear a lift early
  GET  /admin/kill-now/{id} — confirm page for one-click kill
  POST /admin/kill-now/{id} — execute the kill

Auth: Bearer tq_* token in Authorization header — must match the key being lifted.
Kill-now uses itsdangerous URLSafeTimedSerializer (salt "kill-now", 24h expiry).
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourniquet.billing.caps import get_today_spend
from tourniquet.config import settings
from tourniquet.db import get_session
from tourniquet.models import ApiKey

router = APIRouter(prefix="/admin")

_KILL_NOW_EXPIRY_SECONDS = 24 * 60 * 60  # 24 hours


def _kill_now_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="kill-now")


def build_kill_now_url(key_id: str) -> str:
    """Return a signed, 24h-expiry kill-now URL for the given key UUID string."""
    token = _kill_now_signer().dumps(key_id)
    return f"{settings.app_base_url}/admin/kill-now/{key_id}?token={token}"


async def _apply_kill_now(key_id: uuid.UUID) -> tuple[str, int]:
    """Set kill_enabled=True and clamp daily_cap to today's spend.

    Returns (key_name, new_cap_cents).
    """
    today = date.today()
    async with get_session() as session:
        key = await session.get(ApiKey, key_id)
        if key is None:
            raise HTTPException(status_code=404, detail="Key not found")

        today_spend = await get_today_spend(key.id, today, session)
        # Clamp to current cap if spend is 0 (avoid setting cap to 0)
        new_cap = max(today_spend, 1)

        key.kill_enabled = True
        key.daily_cap_usd_cents = new_cap
        await session.commit()

        return key.name, new_cap


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


# ── Kill-now magic-link endpoints ──────────────────────────────────────────────

@router.get("/kill-now/{key_id}")
async def kill_now_confirm(request: Request, key_id: uuid.UUID, token: str) -> HTMLResponse:
    """Magic-link confirmation page. Verifies the token, then shows a confirm button."""
    try:
        payload_id: str = _kill_now_signer().loads(token, max_age=_KILL_NOW_EXPIRY_SECONDS)
    except SignatureExpired:
        raise HTTPException(status_code=400, detail="Kill-now link has expired (24h). Request a new alert.")
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid kill-now token.")

    if payload_id != str(key_id):
        raise HTTPException(status_code=400, detail="Token key mismatch.")

    async with get_session() as session:
        key = await session.get(ApiKey, key_id)
        if key is None:
            raise HTTPException(status_code=404, detail="Key not found")
        key_name = key.name

    dashboard_url = f"{settings.app_base_url}/dashboard/key/{key_id}"
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Kill {key_name}? — Tourniquet</title>
<style>
  body {{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 1rem;color:#111}}
  h1 {{font-size:1.5rem;margin-bottom:.5rem}}
  .warning {{background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:.75rem 1rem;margin:1rem 0}}
  .btn-danger {{background:#dc2626;color:#fff;border:none;padding:.6rem 1.4rem;border-radius:6px;
                font-size:1rem;cursor:pointer;font-weight:600}}
  .btn-danger:hover {{background:#b91c1c}}
  .cancel {{display:inline-block;margin-left:1rem;color:#555;text-decoration:none}}
</style></head>
<body>
<h1>🛑 Kill <em>{key_name}</em>?</h1>
<div class="warning">
  This will set <strong>kill_enabled = True</strong> and clamp the daily cap to
  today's current spend — so the next request will be blocked immediately (402).
</div>
<form method="post">
  <input type="hidden" name="token" value="{token}">
  <button type="submit" class="btn-danger">Confirm kill</button>
  <a href="{dashboard_url}" class="cancel">Cancel</a>
</form>
</body></html>""")


@router.post("/kill-now/{key_id}")
async def kill_now_apply(
    request: Request,
    key_id: uuid.UUID,
    token: str = Form(...),
) -> HTMLResponse:
    """Execute the kill: sets kill_enabled=True and clamps cap to today's spend."""
    try:
        payload_id: str = _kill_now_signer().loads(token, max_age=_KILL_NOW_EXPIRY_SECONDS)
    except SignatureExpired:
        raise HTTPException(status_code=400, detail="Kill-now link has expired (24h).")
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid kill-now token.")

    if payload_id != str(key_id):
        raise HTTPException(status_code=400, detail="Token key mismatch.")

    key_name, new_cap = await _apply_kill_now(key_id)

    dashboard_url = f"{settings.app_base_url}/dashboard/key/{key_id}"
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Killed — Tourniquet</title>
<style>
  body {{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 1rem;color:#111}}
  h1 {{font-size:1.5rem;color:#16a34a}}
  .btn {{display:inline-block;background:#2563eb;color:#fff;padding:.5rem 1.2rem;
         border-radius:6px;text-decoration:none;font-weight:600;margin-top:1rem}}
</style></head>
<body>
<h1>✓ Killed.</h1>
<p>The next request on <strong>{key_name}</strong> will be blocked (402).
   Daily cap clamped to today's spend ({new_cap} cents).</p>
<p>You can adjust the cap or re-enable the kill switch on the dashboard.</p>
<a href="{dashboard_url}" class="btn">Open dashboard →</a>
</body></html>""")
