"""Admin routes — cap lift management, kill-now, and recovery magic links.

Endpoints:
  POST /admin/lift                — temporarily raise a key's daily cap
  POST /admin/unlift              — clear a lift early
  GET  /admin/kill-now/{id}       — confirm page for one-click kill
  POST /admin/kill-now/{id}       — execute the kill
  GET  /admin/lift-by-amount/{id} — confirm page for one-click +$N recovery
  POST /admin/lift-by-amount/{id} — execute the +$N recovery lift

Auth: Bearer tq_* token in Authorization header — must match the key being lifted.
Kill-now / lift-by-amount use itsdangerous URLSafeTimedSerializer with separate
salts ("kill-now", "lift-by-amount") and 24h expiry.
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
from tourniquet.models import ApiKey, ApiKeyAction

router = APIRouter(prefix="/admin")

_KILL_NOW_EXPIRY_SECONDS = 24 * 60 * 60  # 24 hours


def _kill_now_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="kill-now")


def _lift_by_amount_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="lift-by-amount")


def _lift_mode_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="lift-mode")


def _token_sig(token: str) -> str:
    """Extract the HMAC tag (last dot-separated segment) from a URLSafeTimedSerializer token.

    This is stable per token and cheap to compare — used as the replay-guard key
    stored in api_key_actions.details->>'token_sig'.
    """
    return token.rsplit(".", 1)[-1]


async def _assert_token_unused(
    session: AsyncSession, key_id: uuid.UUID, action_type: str, token_sig: str
) -> None:
    """Raise HTTP 400 if this token signature has already been recorded for this key+action.

    Uses api_key_actions as the consumption ledger — no schema change needed.
    The token_sig is the HMAC tag extracted from the itsdangerous token, stable per issuance.
    """
    result = await session.execute(
        select(ApiKeyAction).where(
            ApiKeyAction.api_key_id == key_id,
            ApiKeyAction.action == action_type,
            ApiKeyAction.details["token_sig"].as_string() == token_sig,
        )
    )
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="This link has already been used.")


def build_lift_mode_url(key_id: str, mode: str) -> str:
    """Return a signed, 24h-expiry magic-link URL for a 2x or ceiling lift.

    Token encodes (key_id, mode) — non-malleable.
    """
    token = _lift_mode_signer().dumps([key_id, mode])
    return f"{settings.app_base_url}/admin/lift-mode/{key_id}?token={token}&mode={mode}"


def build_kill_now_url(key_id: str) -> str:
    """Return a signed, 24h-expiry kill-now URL for the given key UUID string."""
    token = _kill_now_signer().dumps(key_id)
    return f"{settings.app_base_url}/admin/kill-now/{key_id}?token={token}"


def build_lift_by_amount_url(key_id: str, amount_cents: int) -> str:
    """Return a signed, 24h-expiry +$N recovery URL.

    Token encodes (key_id, amount_cents) — non-malleable, can't be replayed
    for a different amount.
    """
    token = _lift_by_amount_signer().dumps([key_id, amount_cents])
    return f"{settings.app_base_url}/admin/lift-by-amount/{key_id}?token={token}&amount={amount_cents}"


async def _apply_lift_by_amount(
    key_id: uuid.UUID,
    amount_cents: int,
    source: str = "web",
    token_sig: str | None = None,
) -> tuple[str, int, int]:
    """Add `amount_cents` to today's cap. Clamped to absolute_ceiling.

    Sets `lifted_cap_usd_cents` (not daily_cap) so the lift naturally expires
    at midnight UTC. If a lift is already active, adds to it.

    Records an audit row tagged `source` so the dashboard history shows which
    channel triggered this (slack_socket / telegram_poll / web / cli).

    Returns (key_name, new_lifted_cap_cents, ceiling_clamped_bool_int).
    """
    from tourniquet.audit import ACTION_LIFT_BY_AMOUNT, record_action

    async with get_session() as session:
        key = await session.get(ApiKey, key_id)
        if key is None:
            raise HTTPException(status_code=404, detail="Key not found")

        base = key.lifted_cap_usd_cents if key.lifted_cap_usd_cents is not None else key.daily_cap_usd_cents
        proposed = base + amount_cents
        ceiling = key.absolute_ceiling_usd_cents
        clamped = proposed > ceiling
        new_lifted = min(proposed, ceiling)

        # Lift expires at the next midnight UTC (matches `tourniquet lift` default)
        now = datetime.now(timezone.utc)
        tomorrow = now.date() + timedelta(days=1)
        expires_at = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)

        lifted_before = key.lifted_cap_usd_cents
        key.lifted_cap_usd_cents = new_lifted
        key.lift_expires_at = expires_at

        amt_label = f"${amount_cents // 100}" if amount_cents % 100 == 0 else f"${amount_cents / 100:.2f}"
        summary = (
            f"+{amt_label} bump via {source} — cap now ${new_lifted / 100:.2f} until midnight UTC"
            + (" (ceiling-clamped)" if clamped else "")
        )
        action_details: dict = {
            "amount_cents": amount_cents,
            "lifted_before_cents": lifted_before,
            "lifted_after_cents": new_lifted,
            "ceiling_clamped": bool(clamped),
        }
        if token_sig is not None:
            action_details["token_sig"] = token_sig
        await record_action(
            session, key.id, ACTION_LIFT_BY_AMOUNT, source, summary,
            details=action_details,
        )
        await session.commit()

        return key.name, new_lifted, int(clamped)


async def _apply_kill_now(key_id: uuid.UUID, source: str = "web", token_sig: str | None = None) -> tuple[str, int]:
    """Emergency stop: block today's requests and preserve the daily_cap baseline.

    Sets `lifted_cap_usd_cents` (not `daily_cap_usd_cents`) to today's spend,
    with `lift_expires_at = next midnight UTC`. The proxy's `_effective_cap()`
    logic returns the lifted cap while it's active, so requests are blocked.
    When the lift expires at midnight, the original `daily_cap` re-activates
    automatically — no manual reset required.

    This design preserves the user's configured baseline (daily_cap):
    - Daily_cap is the "normal" quota, persistent across days
    - Lifted_cap is a temporary override with auto-expiry, used for emergency
      stops and in-app recovery bumps. It shadows daily_cap while active.

    Historical context: clamping daily_cap to today_spend permanently destroyed
    the user's quota — after one kill, the key was stuck at 1¢ until the user
    manually re-set daily_cap. This approach avoids that tragedy by leaving
    daily_cap untouched; the kill is temporary by design.

    Returns (key_name, new_effective_cap_cents).
    """
    from tourniquet.audit import ACTION_KILL_NOW, record_action

    today = date.today()
    now = datetime.now(timezone.utc)
    tomorrow = now.date() + timedelta(days=1)
    expires_at = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)

    async with get_session() as session:
        key = await session.get(ApiKey, key_id)
        if key is None:
            raise HTTPException(status_code=404, detail="Key not found")

        today_spend = await get_today_spend(key.id, today, session)
        # Floor at 1¢ so the proxy's percentage math doesn't divide by zero
        # (and so format_money never has to render "$0.00 / $0.00").
        new_lifted = max(today_spend, 1)

        # Capture the cap that WAS effective before the kill, for the audit log
        previous_lifted = key.lifted_cap_usd_cents
        previous_effective = previous_lifted if previous_lifted is not None else key.daily_cap_usd_cents

        key.kill_enabled = True
        key.lifted_cap_usd_cents = new_lifted
        key.lift_expires_at = expires_at
        # daily_cap_usd_cents intentionally untouched — the user's baseline
        # quota survives the kill and is restored automatically at midnight UTC.

        already_floored = previous_effective <= new_lifted
        summary = (
            f"Kill via {source} — "
            + (
                f"effective cap already at floor (${previous_effective / 100:.2f}); "
                "lift refreshed until midnight UTC, daily_cap preserved at "
                f"${key.daily_cap_usd_cents / 100:.2f}"
                if already_floored
                else f"lifted_cap clamped to ${new_lifted / 100:.2f} until midnight UTC; "
                f"daily_cap preserved at ${key.daily_cap_usd_cents / 100:.2f}"
            )
        )
        kill_details: dict = {
            "lifted_before_cents": previous_lifted,
            "lifted_after_cents": new_lifted,
            "daily_cap_cents_preserved": key.daily_cap_usd_cents,
            "today_spend_cents": today_spend,
            "lift_expires_at": expires_at.isoformat(),
            "already_floored": already_floored,
        }
        if token_sig is not None:
            kill_details["token_sig"] = token_sig
        await record_action(
            session, key.id, ACTION_KILL_NOW, source, summary,
            details=kill_details,
        )
        await session.commit()

        return key.name, new_lifted


async def _apply_lift(key_id: uuid.UUID, mode: str, source: str = "web", token_sig: str | None = None) -> str | None:
    """Apply a mode-based cap lift (`2x` / `ceiling` / `ignore`) until midnight UTC.

    Sets `lifted_cap_usd_cents` (not `daily_cap_usd_cents`) with auto-expiry
    at midnight UTC. Each mode doubles the baseline or jumps to the absolute
    ceiling, clamped to prevent overage.

    The `mode == "ignore"` branch still records an audit entry (marked no-op),
    so the dashboard history shows the user explicitly dismissed that alert
    — useful for understanding user intent over time.

    Used by Slack/Telegram in-app callbacks (users tap a +$ button in a message).
    Records an audit row tagged with `source` (slack_socket / telegram_poll / web)
    so the dashboard history tracks which channel triggered the lift.

    Returns the key name on success, or None when the key wasn't found.
    (Returning None instead of raising matches the Telegram callback wrapper's
    prior contract — no need to throw exceptions for missing keys during async
    callbacks.)
    """
    from tourniquet.audit import ACTION_LIFT_MODE, record_action

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        key = await session.get(ApiKey, key_id)
        if key is None:
            return None

        if mode == "ignore":
            await record_action(
                session, key.id, ACTION_LIFT_MODE, source,
                f"Ignored alert via {source} — no cap change",
                details={"mode": "ignore"},
            )
            await session.commit()
            return key.name

        if mode == "2x":
            lifted = int(key.daily_cap_usd_cents * 2)
        elif mode == "ceiling":
            lifted = key.absolute_ceiling_usd_cents
        else:
            return key.name  # unknown mode — skip silently

        lifted = min(lifted, key.absolute_ceiling_usd_cents)
        tomorrow = now.date() + timedelta(days=1)
        expires_at = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)

        lifted_before = key.lifted_cap_usd_cents
        key.lifted_cap_usd_cents = lifted
        key.lift_expires_at = expires_at

        summary = f"Lift {mode} via {source} — cap now ${lifted / 100:.2f} until midnight UTC"
        lift_mode_details: dict = {
            "mode": mode,
            "lifted_before_cents": lifted_before,
            "lifted_after_cents": lifted,
        }
        if token_sig is not None:
            lift_mode_details["token_sig"] = token_sig
        await record_action(
            session, key.id, ACTION_LIFT_MODE, source, summary,
            details=lift_mode_details,
        )
        await session.commit()
        return key.name


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

    sig = _token_sig(token)
    async with get_session() as session:
        await _assert_token_unused(session, key_id, "kill_now", sig)

    key_name, new_cap = await _apply_kill_now(key_id, token_sig=sig)

    # Fire a recovery alert offering one-click bumps via every configured channel
    try:
        await _fire_recovery_alert(key_id, key_name, new_cap)
    except Exception:
        # Recovery alert is best-effort — never block the success page on it
        pass

    # Inline recovery buttons on the success page so user can act without leaving the browser
    from tourniquet.alerts.notifier import recovery_amounts_cents
    bumps = recovery_amounts_cents(new_cap)
    bump_buttons_html = "\n".join(
        f'<a href="{build_lift_by_amount_url(str(key_id), c)}" class="btn-bump">+${c // 100 if c % 100 == 0 else f"{c/100:.2f}"}</a>'
        for c in bumps
    )

    dashboard_url = f"{settings.app_base_url}/dashboard/key/{key_id}"
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Killed — Tourniquet</title>
<style>
  body {{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 1rem;color:#111}}
  h1 {{font-size:1.5rem;color:#16a34a}}
  .recover {{background:#f0f9ff;border:1px solid #93c5fd;border-radius:6px;padding:1rem;margin:1.5rem 0}}
  .btn {{display:inline-block;background:#2563eb;color:#fff;padding:.5rem 1.2rem;
         border-radius:6px;text-decoration:none;font-weight:600;margin-top:1rem}}
  .btn-bump {{display:inline-block;background:#16a34a;color:#fff;padding:.5rem 1rem;
              border-radius:6px;text-decoration:none;font-weight:600;margin:.25rem .5rem .25rem 0;
              font-size:1.1rem}}
  .btn-bump:hover {{background:#15803d}}
</style></head>
<body>
<h1>✓ Killed.</h1>
<p>The next request on <strong>{key_name}</strong> will be blocked (402).
   Daily cap clamped to <strong>${new_cap / 100:.2f}</strong> (today's spend).</p>

<div class="recover">
  <p style="margin:0 0 .5rem 0"><strong>Need a little more to finish?</strong> Bump the cap and continue:</p>
  {bump_buttons_html}
  <p style="margin:.5rem 0 0 0;font-size:.85rem;color:#555">Each lift is a 24h temporary raise — auto-expires at midnight UTC.</p>
</div>

<a href="{dashboard_url}" class="btn">Open dashboard →</a>
</body></html>""")


async def _fire_recovery_alert(key_id: uuid.UUID, key_name: str, new_cap_cents: int) -> None:
    """Send a 'killed, want to bump?' notification through every configured channel."""
    from datetime import date

    from tourniquet.alerts.notifier import AlertEvent, fan_out

    # Fetch alert_email + display currency for the event
    async with get_session() as session:
        key = await session.get(ApiKey, key_id)
        alert_email = key.alert_email if key else None

    event = AlertEvent(
        api_key_name=key_name,
        threshold_pct=-1,
        spent_usd_cents=new_cap_cents,
        cap_usd_cents=new_cap_cents,
        display_currency=settings.display_currency,
        today=date.today(),
        api_key_id=str(key_id),
        alert_email=alert_email,
        recovery_offer=True,
    )
    # kill_enabled doesn't gate kill_now_url anymore (always attached) but explicit here
    await fan_out(event, kill_enabled=True)


# ── /admin/lift-mode/{key_id} ─────────────────────────────────────────────────

@router.get("/lift-mode/{key_id}")
async def lift_mode_confirm(
    request: Request, key_id: uuid.UUID, token: str, mode: str
) -> HTMLResponse:
    """Confirm-page for a 2x or ceiling magic-link lift."""
    try:
        payload = _lift_mode_signer().loads(token, max_age=_KILL_NOW_EXPIRY_SECONDS)
    except SignatureExpired:
        raise HTTPException(status_code=400, detail="Lift link has expired (24h).")
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid lift token.")

    payload_id, payload_mode = payload[0], payload[1]
    if payload_id != str(key_id) or payload_mode != mode or mode not in ("2x", "ceiling"):
        raise HTTPException(status_code=400, detail="Token mismatch.")

    async with get_session() as session:
        key = await session.get(ApiKey, key_id)
        if key is None:
            raise HTTPException(status_code=404, detail="Key not found")
        key_name = key.name
        current_cap = key.lifted_cap_usd_cents or key.daily_cap_usd_cents
        if mode == "2x":
            new_cap = min(current_cap * 2, key.absolute_ceiling_usd_cents)
            label = f"2× — ${new_cap / 100:.2f}"
        else:
            new_cap = key.absolute_ceiling_usd_cents
            label = f"to ceiling — ${new_cap / 100:.2f}"

    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Lift {key_name}? — Tourniquet</title>
<style>
  body {{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 1rem;color:#111}}
  h1 {{font-size:1.4rem}}
  .info {{background:#f0f9ff;border:1px solid #93c5fd;border-radius:6px;padding:.75rem 1rem;margin:1rem 0}}
  .btn {{background:#16a34a;color:#fff;border:none;padding:.6rem 1.4rem;border-radius:6px;
         font-size:1rem;cursor:pointer;font-weight:600}}
  .btn:hover {{background:#15803d}}
  .cancel {{margin-left:1rem;color:#555;text-decoration:none}}
</style></head>
<body>
<h1>Lift <em>{key_name}</em> {label}?</h1>
<div class="info">
  Current cap: ${current_cap / 100:.2f} → After lift: <strong>${new_cap / 100:.2f}</strong>.
  Auto-expires at midnight UTC.
</div>
<form method="post">
  <input type="hidden" name="token" value="{token}">
  <input type="hidden" name="mode" value="{mode}">
  <button type="submit" class="btn">Confirm lift</button>
  <a href="{settings.app_base_url}/dashboard/key/{key_id}" class="cancel">Cancel</a>
</form>
</body></html>""")


@router.post("/lift-mode/{key_id}")
async def lift_mode_apply(
    request: Request,
    key_id: uuid.UUID,
    token: str = Form(...),
    mode: str = Form(...),
) -> HTMLResponse:
    """Execute a 2x or ceiling magic-link lift."""
    try:
        payload = _lift_mode_signer().loads(token, max_age=_KILL_NOW_EXPIRY_SECONDS)
    except SignatureExpired:
        raise HTTPException(status_code=400, detail="Lift link has expired (24h).")
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid lift token.")

    if payload[0] != str(key_id) or payload[1] != mode or mode not in ("2x", "ceiling"):
        raise HTTPException(status_code=400, detail="Token mismatch.")

    sig = _token_sig(token)
    async with get_session() as session:
        await _assert_token_unused(session, key_id, "lift_mode", sig)

        key = await session.get(ApiKey, key_id)
        if key is None:
            raise HTTPException(status_code=404, detail="Key not found")

        lifted_before = key.lifted_cap_usd_cents
        if mode == "2x":
            new_cap = min(key.daily_cap_usd_cents * 2, key.absolute_ceiling_usd_cents)
        else:
            new_cap = key.absolute_ceiling_usd_cents

        now = datetime.now(timezone.utc)
        tomorrow = now.date() + timedelta(days=1)
        expires_at = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)
        key.lifted_cap_usd_cents = new_cap
        key.lift_expires_at = expires_at
        key_name = key.name

        from tourniquet.audit import ACTION_LIFT_MODE, record_action
        lift_mode_details: dict = {
            "mode": mode,
            "lifted_before_cents": lifted_before,
            "lifted_after_cents": new_cap,
            "token_sig": sig,
        }
        summary = f"Lift {mode} via web — cap now ${new_cap / 100:.2f} until midnight UTC"
        await record_action(session, key.id, ACTION_LIFT_MODE, "web", summary, details=lift_mode_details)
        await session.commit()

    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Lifted — Tourniquet</title>
<style>body{{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 1rem}}
h1{{color:#16a34a}}.btn{{display:inline-block;background:#2563eb;color:#fff;padding:.5rem 1.2rem;
border-radius:6px;text-decoration:none;font-weight:600;margin-top:1rem}}</style></head><body>
<h1>✓ Lifted.</h1>
<p><strong>{key_name}</strong> daily cap raised to <strong>${new_cap / 100:.2f}</strong>
   until midnight UTC.</p>
<a href="{settings.app_base_url}/dashboard/key/{key_id}" class="btn">Open dashboard →</a>
</body></html>""")


# ── /admin/lift-by-amount/{key_id} ────────────────────────────────────────────

@router.get("/lift-by-amount/{key_id}")
async def lift_by_amount_confirm(
    request: Request, key_id: uuid.UUID, token: str, amount: int
) -> HTMLResponse:
    """Confirm-page for the +$N recovery lift magic link."""
    try:
        payload = _lift_by_amount_signer().loads(token, max_age=_KILL_NOW_EXPIRY_SECONDS)
    except SignatureExpired:
        raise HTTPException(status_code=400, detail="Recovery link has expired (24h).")
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid recovery token.")

    payload_id, payload_amount = payload[0], int(payload[1])
    if payload_id != str(key_id):
        raise HTTPException(status_code=400, detail="Token key mismatch.")
    if payload_amount != amount:
        raise HTTPException(status_code=400, detail="Token amount mismatch.")

    async with get_session() as session:
        key = await session.get(ApiKey, key_id)
        if key is None:
            raise HTTPException(status_code=404, detail="Key not found")
        key_name = key.name
        current_cap = key.lifted_cap_usd_cents or key.daily_cap_usd_cents

    new_cap_after = min(current_cap + amount, 0)  # placeholder for label below
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Bump cap — Tourniquet</title>
<style>
  body {{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 1rem;color:#111}}
  h1 {{font-size:1.4rem}}
  .info {{background:#f0f9ff;border:1px solid #93c5fd;border-radius:6px;padding:.75rem 1rem;margin:1rem 0}}
  .btn {{background:#16a34a;color:#fff;border:none;padding:.6rem 1.4rem;border-radius:6px;
         font-size:1rem;cursor:pointer;font-weight:600}}
  .btn:hover {{background:#15803d}}
  .cancel {{margin-left:1rem;color:#555;text-decoration:none}}
</style></head>
<body>
<h1>Bump <em>{key_name}</em> by ${amount / 100:.2f}?</h1>
<div class="info">
  Current cap: <strong>${current_cap / 100:.2f}</strong> →
  After bump: <strong>${(current_cap + amount) / 100:.2f}</strong><br>
  Lift auto-expires at midnight UTC (24h max).
</div>
<form method="post">
  <input type="hidden" name="token" value="{token}">
  <input type="hidden" name="amount" value="{amount}">
  <button type="submit" class="btn">Confirm bump</button>
  <a href="{settings.app_base_url}/dashboard/key/{key_id}" class="cancel">Cancel</a>
</form>
</body></html>""")


@router.post("/lift-by-amount/{key_id}")
async def lift_by_amount_apply(
    request: Request,
    key_id: uuid.UUID,
    token: str = Form(...),
    amount: int = Form(...),
) -> HTMLResponse:
    """Execute the +$N recovery lift."""
    try:
        payload = _lift_by_amount_signer().loads(token, max_age=_KILL_NOW_EXPIRY_SECONDS)
    except SignatureExpired:
        raise HTTPException(status_code=400, detail="Recovery link has expired (24h).")
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid recovery token.")

    payload_id, payload_amount = payload[0], int(payload[1])
    if payload_id != str(key_id) or payload_amount != amount:
        raise HTTPException(status_code=400, detail="Token mismatch.")

    sig = _token_sig(token)
    async with get_session() as session:
        await _assert_token_unused(session, key_id, "lift_by_amount", sig)

    key_name, new_lifted, ceiling_clamped = await _apply_lift_by_amount(key_id, amount, token_sig=sig)

    note = " (clamped to ceiling)" if ceiling_clamped else ""
    dashboard_url = f"{settings.app_base_url}/dashboard/key/{key_id}"
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Bumped — Tourniquet</title>
<style>
  body {{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 1rem;color:#111}}
  h1 {{font-size:1.5rem;color:#16a34a}}
  .btn {{display:inline-block;background:#2563eb;color:#fff;padding:.5rem 1.2rem;
         border-radius:6px;text-decoration:none;font-weight:600;margin-top:1rem}}
</style></head>
<body>
<h1>✓ Cap bumped.</h1>
<p><strong>{key_name}</strong> can now spend up to
   <strong>${new_lifted / 100:.2f}</strong> today{note}.</p>
<p>The lift expires at midnight UTC. After that, the original daily cap returns.</p>
<a href="{dashboard_url}" class="btn">Open dashboard →</a>
</body></html>""")
