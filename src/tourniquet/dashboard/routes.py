"""Dashboard routes — HTMX/Jinja2, localhost-only (no auth required).

Localhost is the trust boundary; no session/magic-link auth.
"""

from __future__ import annotations

import logging
import math
import platform
import secrets
import subprocess
import uuid

log = logging.getLogger(__name__)
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import bcrypt
from cryptography.fernet import Fernet
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tourniquet.analytics.insights import compute_insights
from tourniquet.billing.caps import get_today_spend
from tourniquet.billing.formatting import format_money, from_major_units
from tourniquet.billing.profiles import PROFILES
from tourniquet.billing.suggestions import InsufficientHistory, suggest_from_history
from tourniquet.config import settings
from tourniquet.db import get_session
from tourniquet.models import ApiKey, ApiKeyAction, UsageEvent

router = APIRouter()
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.filters["format_money_filter"] = lambda cents: format_money(
    int(cents), settings.display_currency
)


def _static_version() -> str:
    """Return a cache-buster suffix for CSS/JS links.

    Uses the mtime of main.css so any change to the stylesheet during development
    forces browsers to refetch on the next page load — no hard refresh needed.
    Also rotates each server start (mtime changes when files are touched).
    """
    try:
        css = Path(__file__).resolve().parent.parent / "static" / "css" / "main.css"
        return str(int(css.stat().st_mtime))
    except OSError:
        return "1"


templates.env.globals["static_version"] = _static_version


def _device_label(request: Request) -> str:
    """Map the User-Agent to a human label for the trust badge."""
    ua = request.headers.get("user-agent", "")
    if "Windows" in ua:
        return "PC"
    if "Macintosh" in ua or "Mac OS X" in ua:
        return "Mac"
    if "Android" in ua or "iPhone" in ua or "iPad" in ua:
        return "device"
    if "Linux" in ua:
        return "machine"
    return "machine"


# Available in any template as {{ device_label(request) }}
templates.env.globals["device_label"] = _device_label

_fernet = Fernet(settings.fernet_key.encode())


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_tq_token() -> str:
    return "tq_" + secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    return bcrypt.hashpw(token.encode(), bcrypt.gensalt()).decode()


def _encrypt_anthropic_key(raw_key: str) -> str:
    return _fernet.encrypt(raw_key.encode()).decode()


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _default_shell(request: Request) -> str:
    """Guess the user's shell from their browser User-Agent."""
    ua = request.headers.get("user-agent", "")
    if "Windows" in ua:
        return "powershell"
    return "bash"


async def _get_key_or_404(key_id: uuid.UUID, session: AsyncSession) -> ApiKey:
    result = await session.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="Key not found")
    return key


async def _key_summary(key: ApiKey, today: date, session: AsyncSession) -> dict[str, Any]:
    spent = await get_today_spend(key.id, today, session)
    cap = key.daily_cap_usd_cents
    currency = settings.display_currency

    # Effective cap: use lifted cap if active
    lifted = getattr(key, "lifted_cap_usd_cents", None)
    lift_expires = getattr(key, "lift_expires_at", None)
    now = datetime.now(timezone.utc)
    # SQLite returns naive datetimes; normalise to UTC before comparing
    if lift_expires is not None and lift_expires.tzinfo is None:
        lift_expires = lift_expires.replace(tzinfo=timezone.utc)
    effective_cap = cap
    lift_active = False
    if lifted and lift_expires and lift_expires > now:
        effective_cap = lifted
        lift_active = True

    pct = int(spent / effective_cap * 100) if effective_cap else 0

    return {
        "id": str(key.id),
        "name": key.name,
        "profile": key.profile,
        "daily_cap_usd_cents": cap,
        "daily_cap_display": format_money(cap, currency),
        "effective_cap_usd_cents": effective_cap,
        "effective_cap_display": format_money(effective_cap, currency),
        "spent_usd_cents": spent,
        "spent_display": format_money(spent, currency),
        "pct": min(pct, 100),
        "kill_enabled": key.kill_enabled,
        "auto_tune_mode": key.auto_tune_mode,
        "absolute_ceiling_usd_cents": key.absolute_ceiling_usd_cents,
        "lift_active": lift_active,
        "lifted_cap_usd_cents": lifted,
        "lift_expires_at": lift_expires,
        "currency": currency,
    }


async def _get_daily_totals(key_id: uuid.UUID, days: int, session: AsyncSession) -> list[int]:
    """Daily spend totals for last N days (0 for empty days)."""
    cutoff = date.today() - timedelta(days=days)
    result = await session.execute(
        text("""
            SELECT DATE(created_at) as day, SUM(cost_usd_cents) as total
            FROM usage_events
            WHERE api_key_id = :kid AND created_at >= :cutoff
            GROUP BY day ORDER BY day
        """),
        {"kid": str(key_id), "cutoff": cutoff},
    )
    rows = {r[0]: int(r[1]) for r in result.all()}
    today = date.today()
    return [rows.get(str(today - timedelta(days=i)), 0) for i in range(days - 1, -1, -1)]


async def _get_heatmap_data(key_id: uuid.UUID, session: AsyncSession) -> list[list[int]]:
    """7 rows (Mon–Sun) × 24 cols. Values: 0 = no spend, 1–5 = intensity."""
    result = await session.execute(
        text("""
            SELECT
              CAST(strftime('%w', datetime(created_at)) AS INTEGER) as dow_sun,
              CAST(strftime('%H', datetime(created_at)) AS INTEGER) as hr,
              SUM(cost_usd_cents) as cost
            FROM usage_events
            WHERE api_key_id = :kid
              AND created_at >= datetime('now', '-28 days')
            GROUP BY dow_sun, hr
        """),
        {"kid": str(key_id)},
    )
    rows = result.all()
    grid: list[list[int]] = [[0] * 24 for _ in range(7)]
    if not rows:
        return grid
    max_val = max((int(r[2]) for r in rows), default=1)
    for r in rows:
        dow_sun = int(r[0])
        hr = int(r[1])
        cost = int(r[2])
        weekday = (dow_sun - 1) % 7  # convert Sun=0 → Mon=0
        intensity = max(1, min(5, math.ceil(cost / max_val * 5)))
        grid[weekday][hr] = intensity
    return grid


def _alert_channel_status() -> dict[str, dict[str, Any]]:
    """Inspect settings and return per-channel configured/tier status.

    Powers the "Getting started" checklist on the dashboard so each row can
    show ✓ green when the channel will actually deliver an alert.

    For each channel:
      configured: True when the channel will fire on a real cap-hit
      tier:       short label describing the active path (e.g. "in-app one-tap"
                  vs "browser-confirm webhook" for Slack)
    """
    slack_bot_full = bool(
        settings.slack_app_token
        and getattr(settings, "slack_bot_token", "")
        and getattr(settings, "slack_channel_id", "")
    )
    slack_webhook = bool(settings.slack_webhook_url)
    slack_configured = slack_bot_full or slack_webhook
    if slack_bot_full:
        slack_tier = "in-app one-tap (Socket Mode + bot post)"
    elif slack_webhook:
        slack_tier = "webhook + browser-confirm taps"
    else:
        slack_tier = ""

    telegram_configured = bool(
        getattr(settings, "telegram_bot_token", "")
        and getattr(settings, "telegram_chat_id", "")
    )
    email_configured = bool(
        getattr(settings, "resend_api_key", "")
        and getattr(settings, "resend_from_email", "")
    )
    webhook_configured = bool(getattr(settings, "alert_webhook_url", ""))
    desktop_configured = (
        str(getattr(settings, "enable_mac_notifications", "")).lower() == "true"
        or str(getattr(settings, "enable_desktop_notifications", "")).lower() == "true"
    )

    return {
        "slack": {"configured": slack_configured, "tier": slack_tier},
        "telegram": {"configured": telegram_configured, "tier": "in-app one-tap (long-poll)" if telegram_configured else ""},
        "email": {"configured": email_configured, "tier": "Resend" if email_configured else ""},
        "webhook": {"configured": webhook_configured, "tier": "generic JSON POST" if webhook_configured else ""},
        "desktop": {"configured": desktop_configured, "tier": "OS banner" if desktop_configured else ""},
    }


def _sleep_protection_status() -> dict[str, Any]:
    """Detect whether something is preventing the OS from sleeping.

    On macOS we shell out to `pmset -g assertions` and check for an active
    PreventUserIdleSystemSleep assertion (which `caffeinate -di` creates). On
    Linux/server platforms sleep isn't a concern in the same way, so we
    report a generic 'always on' state.

    Returns a dict consumed by the Getting Started checklist:
      platform: 'darwin' | 'linux' | 'windows' | 'other'
      active:   True if something is keeping the system awake
      owner:    short label describing the wake-lock holder, when known
    """
    sysname = platform.system().lower()
    if sysname == "darwin":
        try:
            result = subprocess.run(
                ["pmset", "-g", "assertions"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            output = result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return {"platform": "darwin", "active": False, "owner": ""}

        active = False
        owner = ""
        for line in output.splitlines():
            stripped = line.strip()
            if "PreventUserIdleSystemSleep" in stripped and stripped.endswith(" 1"):
                active = True
            elif active and "named:" in stripped.lower() and "PreventUserIdleSystemSleep" in stripped:
                # The per-process line that actually holds the assertion we
                # flagged active. Match the assertion type to avoid attributing
                # an unrelated assertion (e.g. NoIdleSleepAssertion from a
                # video-call camera capture) to the wrong process.
                # Format: "   pid 12345(caffeinate): named: \"...\""
                if "caffeinate" in stripped.lower():
                    owner = "caffeinate"
                else:
                    owner = stripped.split("(", 1)[-1].split(")", 1)[0] if "(" in stripped else "unknown process"
                break
        return {"platform": "darwin", "active": active, "owner": owner}

    if sysname == "linux":
        # Best-effort detect via systemd-inhibit. Honest "unknown" beats
        # the previous "always-on by default" lie, which was wrong for
        # any Linux laptop on battery with the default suspend policy.
        try:
            result = subprocess.run(
                ["systemd-inhibit", "--list", "--no-pager"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            stdout_lower = result.stdout.lower()
            if "tourniquet" in stdout_lower or "idle:sleep" in stdout_lower:
                return {"platform": "linux", "active": True, "owner": "systemd-inhibit"}
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        return {"platform": "linux", "active": False, "owner": ""}

    if sysname == "windows":
        # Best-effort detect via powercfg /requests. Admin-only on some
        # installs; tolerate failure with an honest "unknown" rather than
        # the previous "always-on by default" lie.
        try:
            result = subprocess.run(
                ["powercfg", "/requests"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            if "SYSTEM:" in result.stdout:
                tail = result.stdout.split("SYSTEM:", 1)[1].splitlines()
                if len(tail) > 1 and "None." not in tail[1]:
                    return {"platform": "windows", "active": True, "owner": "system-execution-state"}
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        return {"platform": "windows", "active": False, "owner": ""}

    return {"platform": sysname or "other", "active": False, "owner": ""}


async def _get_action_history(
    key_id: uuid.UUID, session: AsyncSession, limit: int = 50,
) -> list[dict[str, Any]]:
    """Last N audit-log entries for this key, newest first.

    Powers the per-key Action history section. Each row shows when an action
    happened, which channel triggered it (slack_socket / telegram_poll / web /
    cli / auto), and a one-line summary. Even no-op actions (e.g. killing a
    key already at minimum cap) appear here, so the operator sees their tap
    landed.
    """
    result = await session.execute(
        select(ApiKeyAction)
        .where(ApiKeyAction.api_key_id == key_id)
        .order_by(desc(ApiKeyAction.created_at))
        .limit(limit)
    )
    rows = []
    for a in result.scalars().all():
        rows.append({
            "ts": a.created_at,
            "action": a.action,
            "source": a.source,
            "summary": a.summary,
            "details": a.details or {},
        })
    return rows


async def _get_alert_log(key_id: uuid.UUID, session: AsyncSession) -> list[dict[str, Any]]:
    """Last 50 cap-hit events from usage_events."""
    result = await session.execute(
        select(UsageEvent)
        .where(UsageEvent.api_key_id == key_id, UsageEvent.cap_hit == True)  # noqa: E712
        .order_by(desc(UsageEvent.created_at))
        .limit(50)
    )
    events = result.scalars().all()
    currency = settings.display_currency
    rows = []
    for e in events:
        try:
            cost_display = format_money(int(e.cost_usd_cents), currency)
        except (TypeError, ValueError):
            cost_display = "—"
        rows.append({
            "ts": e.created_at,
            "model": e.model,
            "cost_display": cost_display,
        })
    return rows


# ── Landing/login passthrough ──────────────────────────────────────────────────

@router.get("/")
async def landing(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "landing.html")


@router.get("/login")
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html")


@router.get("/trust")
async def trust_page(request: Request) -> HTMLResponse:
    """Render the data-residency / trust explainer."""
    return templates.TemplateResponse(request, "trust.html")


# ── Dashboard full page ────────────────────────────────────────────────────────

@router.get("/dashboard")
async def dashboard(request: Request) -> HTMLResponse:
    today = date.today()
    async with get_session() as session:
        result = await session.execute(select(ApiKey))
        keys = result.scalars().all()

        summaries = []
        for k in keys:
            summaries.append(await _key_summary(k, today, session))

        # Build panel context for first key
        panel_ctx: dict[str, Any] = {}
        first_id = summaries[0]["id"] if summaries else None
        if keys:
            first_key = keys[0]
            first_key_id = first_key.id
            daily_totals = await _get_daily_totals(first_key_id, 14, session)
            heatmap = await _get_heatmap_data(first_key_id, session)
            alert_log = await _get_alert_log(first_key_id, session)
            history = await _get_action_history(first_key_id, session)
            suggestion = None
            try:
                suggestion = suggest_from_history(
                    daily_totals,
                    first_key.daily_cap_usd_cents,
                    first_key.absolute_ceiling_usd_cents,
                )
            except InsufficientHistory:
                pass
            insights = await compute_insights(first_key_id, 14, session)
            panel_ctx = {
                "key": summaries[0],
                "key_id": str(first_key_id),
                "daily_totals": daily_totals,
                "heatmap": heatmap,
                "alert_log": alert_log,
                "action_history": history,
                "suggestion": suggestion,
                "insights": insights,
                "auto_tune_modes": ["off", "suggest", "creep"],
            }

    return templates.TemplateResponse(request, "dashboard.html", {
        "keys": summaries,
        "selected_id": first_id,
        "profiles": list(PROFILES.keys()),
        "profiles_obj": PROFILES,
        "currency": settings.display_currency,
        "channel_status": _alert_channel_status(),
        "sleep_protection": _sleep_protection_status(),
        **panel_ctx,
    })


# ── Key main panel ─────────────────────────────────────────────────────────────

@router.get("/dashboard/key/{key_id}")
async def key_panel(request: Request, key_id: uuid.UUID) -> HTMLResponse:
    today = date.today()
    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        summary = await _key_summary(key, today, session)

        daily_totals = await _get_daily_totals(key_id, 14, session)
        heatmap = await _get_heatmap_data(key_id, session)
        alert_log = await _get_alert_log(key_id, session)
        history = await _get_action_history(key_id, session)

        # Suggestion
        suggestion = None
        try:
            suggestion = suggest_from_history(
                daily_totals,
                key.daily_cap_usd_cents,
                key.absolute_ceiling_usd_cents,
            )
        except InsufficientHistory:
            pass

        # Insights for model breakdown
        insights = await compute_insights(key_id, 14, session)

    template = "_partials/key_panel.html" if _is_htmx(request) else "dashboard.html"

    ctx = {
        "key": summary,
        "key_id": str(key_id),
        "daily_totals": daily_totals,
        "heatmap": heatmap,
        "alert_log": alert_log,
        "action_history": history,
        "suggestion": suggestion,
        "insights": insights,
        "profiles": list(PROFILES.keys()),
        "profiles_obj": PROFILES,
        "currency": settings.display_currency,
        "auto_tune_modes": ["off", "suggest", "creep"],
    }

    if _is_htmx(request):
        return templates.TemplateResponse(request, "_partials/key_panel.html", ctx)

    # Full page: also need sidebar keys
    async with get_session() as session:
        result = await session.execute(select(ApiKey))
        all_keys = result.scalars().all()
        today2 = date.today()
        summaries = [await _key_summary(k, today2, session) for k in all_keys]

    ctx["keys"] = summaries
    ctx["selected_id"] = str(key_id)
    ctx["channel_status"] = _alert_channel_status()
    ctx["sleep_protection"] = _sleep_protection_status()
    return templates.TemplateResponse(request, "dashboard.html", ctx)


# ── Charts partial ─────────────────────────────────────────────────────────────

@router.get("/dashboard/key/{key_id}/charts")
async def key_charts(request: Request, key_id: uuid.UUID) -> HTMLResponse:
    async with get_session() as session:
        await _get_key_or_404(key_id, session)
        daily_totals = await _get_daily_totals(key_id, 14, session)
        heatmap = await _get_heatmap_data(key_id, session)
        insights = await compute_insights(key_id, 14, session)

    return templates.TemplateResponse(request, "_partials/charts.html", {
        "key_id": str(key_id),
        "daily_totals": daily_totals,
        "heatmap": heatmap,
        "insights": insights,
        "currency": settings.display_currency,
    })


# ── Live spend bar ─────────────────────────────────────────────────────────────

@router.get("/dashboard/key/{key_id}/spend-now")
async def spend_now(request: Request, key_id: uuid.UUID) -> HTMLResponse:
    today = date.today()
    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        summary = await _key_summary(key, today, session)

    return templates.TemplateResponse(request, "_partials/spend_now.html", {
        "key": summary,
        "key_id": str(key_id),
    })


# ── Alert log tail ─────────────────────────────────────────────────────────────

@router.get("/dashboard/key/{key_id}/alerts")
async def alerts_log(request: Request, key_id: uuid.UUID) -> HTMLResponse:
    async with get_session() as session:
        await _get_key_or_404(key_id, session)
        alert_log = await _get_alert_log(key_id, session)

    return templates.TemplateResponse(request, "_partials/alerts_log.html", {
        "alert_log": alert_log,
        "key_id": str(key_id),
    })


@router.get("/dashboard/key/{key_id}/history")
async def action_history(request: Request, key_id: uuid.UUID) -> HTMLResponse:
    """Per-key audit log of cap-changing actions. HTMX-polled every 10s."""
    async with get_session() as session:
        await _get_key_or_404(key_id, session)
        history = await _get_action_history(key_id, session)

    return templates.TemplateResponse(request, "_partials/action_history.html", {
        "action_history": history,
        "key_id": str(key_id),
    })


# ── Control actions ────────────────────────────────────────────────────────────

@router.post("/dashboard/key/{key_id}/cap")
async def update_cap(
    request: Request,
    key_id: uuid.UUID,
    daily_cap: float = Form(...),
) -> HTMLResponse:
    currency = settings.display_currency
    cents = from_major_units(daily_cap, currency)
    if cents < 1:
        raise HTTPException(status_code=422, detail="Cap must be at least 1 cent")

    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        if cents > key.absolute_ceiling_usd_cents:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cap ({format_money(cents, currency)}) cannot exceed absolute ceiling "
                    f"({format_money(key.absolute_ceiling_usd_cents, currency)}). "
                    "Raise the ceiling first."
                ),
            )
        key.daily_cap_usd_cents = cents
        await session.commit()
        today = date.today()
        summary = await _key_summary(key, today, session)

    return templates.TemplateResponse(request, "_partials/control_panel.html", {
        "key": summary,
        "key_id": str(key_id),
        "profiles": list(PROFILES.keys()),
        "profiles_obj": PROFILES,
        "auto_tune_modes": ["off", "suggest", "creep"],
        "flash": "Cap updated.",
    })


@router.post("/dashboard/key/{key_id}/ceiling")
async def update_ceiling(
    request: Request,
    key_id: uuid.UUID,
    absolute_ceiling: float = Form(...),
) -> HTMLResponse:
    """Update the per-key absolute ceiling.

    Ceiling is the hard upper bound that auto-tune AND the "lift to ceiling"
    button respect. We refuse to set it below the current `daily_cap` because
    that would silently shrink today's enforceable cap to the new ceiling on
    the next request — surprising and bad. The user must lower daily_cap
    first if they want a smaller ceiling.
    """
    currency = settings.display_currency
    cents = from_major_units(absolute_ceiling, currency)
    if cents < 1:
        raise HTTPException(status_code=422, detail="Ceiling must be at least 1 cent")

    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        if cents < key.daily_cap_usd_cents:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Ceiling ({format_money(cents, currency)}) must be ≥ daily cap "
                    f"({format_money(key.daily_cap_usd_cents, currency)}). "
                    "Lower the daily cap first if you want a smaller ceiling."
                ),
            )
        key.absolute_ceiling_usd_cents = cents
        await session.commit()
        today = date.today()
        summary = await _key_summary(key, today, session)

    return templates.TemplateResponse(request, "_partials/control_panel.html", {
        "key": summary,
        "key_id": str(key_id),
        "profiles": list(PROFILES.keys()),
        "profiles_obj": PROFILES,
        "auto_tune_modes": ["off", "suggest", "creep"],
        "flash": "Ceiling updated.",
    })


@router.post("/dashboard/key/{key_id}/kill")
async def toggle_kill(request: Request, key_id: uuid.UUID) -> HTMLResponse:
    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        key.kill_enabled = not key.kill_enabled
        await session.commit()
        today = date.today()
        summary = await _key_summary(key, today, session)

    return templates.TemplateResponse(request, "_partials/control_panel.html", {
        "key": summary,
        "key_id": str(key_id),
        "profiles": list(PROFILES.keys()),
        "profiles_obj": PROFILES,
        "auto_tune_modes": ["off", "suggest", "creep"],
        "flash": f"Kill switch {'enabled' if summary['kill_enabled'] else 'disabled'}.",
    })


@router.post("/dashboard/key/{key_id}/profile")
async def update_profile(
    request: Request,
    key_id: uuid.UUID,
    profile: str = Form(...),
) -> HTMLResponse:
    if profile not in PROFILES:
        raise HTTPException(status_code=422, detail="Invalid profile")

    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        key.profile = profile
        await session.commit()
        today = date.today()
        summary = await _key_summary(key, today, session)

    return templates.TemplateResponse(request, "_partials/control_panel.html", {
        "key": summary,
        "key_id": str(key_id),
        "profiles": list(PROFILES.keys()),
        "profiles_obj": PROFILES,
        "auto_tune_modes": ["off", "suggest", "creep"],
        "flash": "Profile updated.",
    })


@router.post("/dashboard/key/{key_id}/auto-tune")
async def update_auto_tune(
    request: Request,
    key_id: uuid.UUID,
    auto_tune_mode: str = Form(...),
) -> HTMLResponse:
    if auto_tune_mode not in ("off", "suggest", "creep"):
        raise HTTPException(status_code=422, detail="Invalid auto_tune_mode")

    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        key.auto_tune_mode = auto_tune_mode
        await session.commit()
        today = date.today()
        summary = await _key_summary(key, today, session)

    return templates.TemplateResponse(request, "_partials/control_panel.html", {
        "key": summary,
        "key_id": str(key_id),
        "profiles": list(PROFILES.keys()),
        "profiles_obj": PROFILES,
        "auto_tune_modes": ["off", "suggest", "creep"],
        "flash": "Auto-tune updated.",
    })


@router.post("/dashboard/key/{key_id}/lift")
async def lift_cap(
    request: Request,
    key_id: uuid.UUID,
    mode: str = Form("multiplier"),
    multiplier: float = Form(2.0),
) -> HTMLResponse:
    """Lift today's cap. Mode: 'multiplier' (× N) or 'ceiling' (to absolute ceiling)."""
    now = datetime.now(timezone.utc)
    tomorrow = now.date() + timedelta(days=1)
    expires_at = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)

    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        ceiling = key.absolute_ceiling_usd_cents

        if mode == "multiplier":
            raw = int(key.daily_cap_usd_cents * multiplier)
        else:  # ceiling
            raw = ceiling

        lifted_cents = min(raw, ceiling)
        key.lifted_cap_usd_cents = lifted_cents
        key.lift_expires_at = expires_at
        await session.commit()
        today = date.today()
        summary = await _key_summary(key, today, session)

    return templates.TemplateResponse(request, "_partials/control_panel.html", {
        "key": summary,
        "key_id": str(key_id),
        "profiles": list(PROFILES.keys()),
        "profiles_obj": PROFILES,
        "auto_tune_modes": ["off", "suggest", "creep"],
        "flash": f"Cap lifted to {format_money(lifted_cents, settings.display_currency)} until midnight UTC.",
    })


@router.post("/dashboard/key/{key_id}/unlift")
async def unlift_cap(request: Request, key_id: uuid.UUID) -> HTMLResponse:
    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        key.lifted_cap_usd_cents = None
        key.lift_expires_at = None
        await session.commit()
        today = date.today()
        summary = await _key_summary(key, today, session)

    return templates.TemplateResponse(request, "_partials/control_panel.html", {
        "key": summary,
        "key_id": str(key_id),
        "profiles": list(PROFILES.keys()),
        "profiles_obj": PROFILES,
        "auto_tune_modes": ["off", "suggest", "creep"],
        "flash": "Lift cleared. Base cap restored.",
    })


@router.post("/dashboard/key/{key_id}/rotate")
async def rotate_token(request: Request, key_id: uuid.UUID) -> HTMLResponse:
    new_token = _make_tq_token()
    new_hash = _hash_token(new_token)

    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        key.tq_token_hash = new_hash
        await session.commit()

    return templates.TemplateResponse(request, "key_rotated.html", {
        "token": new_token,
        "key_id": str(key_id),
        "key_name": key.name,
        "default_shell": _default_shell(request),
    })


@router.post("/dashboard/key/{key_id}/apply-suggestion")
async def apply_suggestion(request: Request, key_id: uuid.UUID) -> HTMLResponse:
    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        daily_totals = await _get_daily_totals(key_id, 14, session)

        try:
            sug = suggest_from_history(
                daily_totals,
                key.daily_cap_usd_cents,
                key.absolute_ceiling_usd_cents,
            )
            key.daily_cap_usd_cents = sug.suggested_cap_usd_cents
            await session.commit()
            flash = f"Cap set to {format_money(sug.suggested_cap_usd_cents, settings.display_currency)}."
        except InsufficientHistory:
            flash = "Not enough history to apply suggestion."

        today = date.today()
        summary = await _key_summary(key, today, session)

    return templates.TemplateResponse(request, "_partials/control_panel.html", {
        "key": summary,
        "key_id": str(key_id),
        "profiles": list(PROFILES.keys()),
        "profiles_obj": PROFILES,
        "auto_tune_modes": ["off", "suggest", "creep"],
        "flash": flash,
    })


@router.post("/dashboard/key/{key_id}/delete")
async def delete_key(request: Request, key_id: uuid.UUID) -> RedirectResponse:
    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        await session.delete(key)
        await session.commit()
    return RedirectResponse("/dashboard", status_code=303)


# ── Onboarding "intel" routes — used by the post-creation Smart Suggestions UI ─

@router.post("/dashboard/key/{key_id}/intel-monitor")
async def intel_monitor(request: Request, key_id: uuid.UUID) -> HTMLResponse:
    """User chose 'just learn from my usage' — set auto_tune_mode to suggest."""
    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        key.auto_tune_mode = "suggest"
        await session.commit()
    return HTMLResponse(
        '<div class="intel-section intel-result">'
        '<h2 class="next-steps-heading">⏱ Monitoring enabled</h2>'
        '<p>Tourniquet will record every request through this key. After a few days '
        'of traffic, you\'ll see a Suggestion card on the dashboard with a '
        'recommended cap based on your actual usage.</p>'
        '<p class="muted-hint">Auto-tune mode set to <code>suggest</code> — '
        'change anytime from the control panel.</p>'
        '</div>'
    )


@router.post("/dashboard/key/{key_id}/intel-fetch")
async def intel_fetch(request: Request, key_id: uuid.UUID, admin_key: str = Form(...)) -> HTMLResponse:
    """User pasted an admin key — fetch 14 days of cost history, suggest a cap.

    The admin key is held in memory only for this single function call and is
    `del`'d before any error path can leak it. Never written to disk or logged.
    """
    if not admin_key.startswith("sk-ant-admin-"):
        del admin_key
        return HTMLResponse(
            '<div class="intel-section intel-error">'
            '<p class="warn">⚠ Admin keys start with <code>sk-ant-admin-</code>. '
            'That looked like a regular key. Try again or pick a different option.</p>'
            '</div>',
            status_code=400,
        )

    try:
        from tourniquet.anthropic_admin import fetch_cost_history
        from tourniquet.billing.suggestions import (
            InsufficientHistory,
            recommend_profile,
            suggest_from_history,
        )

        try:
            daily_costs = await fetch_cost_history(admin_key, days=14)
        finally:
            del admin_key  # zero from locals before any further code runs

        if not daily_costs:
            return HTMLResponse(
                '<div class="intel-section intel-result">'
                '<h2 class="next-steps-heading">No history found</h2>'
                '<p>Anthropic returned no usage data for the last 14 days. '
                'You\'re probably new to Anthropic — pick "Just monitor my usage" '
                'and Tourniquet will learn from your traffic.</p>'
                '</div>'
            )

        daily_totals = [dc.usd_cents for dc in daily_costs]

        async with get_session() as session:
            key = await _get_key_or_404(key_id, session)
            try:
                sug = suggest_from_history(
                    daily_totals_usd_cents=daily_totals,
                    current_cap_usd_cents=key.daily_cap_usd_cents,
                    absolute_ceiling_usd_cents=key.absolute_ceiling_usd_cents,
                )
            except InsufficientHistory:
                return HTMLResponse(
                    '<div class="intel-section intel-result">'
                    '<h2 class="next-steps-heading">Not enough history</h2>'
                    '<p>You have fewer than 3 days of non-zero usage in the last 14 days. '
                    'Pick "Just monitor my usage" and Tourniquet will learn from your '
                    'traffic going forward.</p>'
                    '</div>'
                )

            currency = settings.display_currency
            avg_cents = int(sum(daily_totals) / len(daily_totals)) if daily_totals else 0
            sorted_totals = sorted([t for t in daily_totals if t > 0])
            p50 = sorted_totals[len(sorted_totals) // 2] if sorted_totals else 0
            p95_idx = max(0, int(len(sorted_totals) * 0.95) - 1) if sorted_totals else 0
            p95 = sorted_totals[p95_idx] if sorted_totals else 0
            mx = max(daily_totals) if daily_totals else 0

        # Build sparkline SVG with the P95 day highlighted
        sparkline = _build_sparkline(daily_totals, p95)
        prof_rec = recommend_profile(daily_totals)

        ceiling_note = (
            '<p class="muted-hint">⚠ Capped by your absolute ceiling — the P95×1.5 number was higher than your safety wall.</p>'
            if sug.capped_by_ceiling else ''
        )
        avg_str = format_money(avg_cents, currency)
        p50_str = format_money(p50, currency)
        p95_str = format_money(p95, currency)
        mx_str = format_money(mx, currency)
        suggested_str = format_money(sug.suggested_cap_usd_cents, currency)
        # Math walkthrough — show the actual numbers, not just the formula
        p95_x_15 = int(round(p95 * 1.5))

        return HTMLResponse(
            f'<div class="intel-section intel-result">'
            f'<h2 class="next-steps-heading">📊 Your last 14 days</h2>'

            # Sparkline + stat strip
            f'<div class="intel-spark-wrap">{sparkline}</div>'
            f'<div class="intel-stats">'
            f'<div class="stat"><span class="stat-label">avg</span><span class="stat-val">{avg_str}</span></div>'
            f'<div class="stat"><span class="stat-label">p50</span><span class="stat-val">{p50_str}</span></div>'
            f'<div class="stat stat-highlight"><span class="stat-label">p95</span><span class="stat-val">{p95_str}</span></div>'
            f'<div class="stat"><span class="stat-label">max</span><span class="stat-val">{mx_str}</span></div>'
            f'</div>'

            # Reasoning block
            f'<h3 class="intel-subhead">💡 Suggested cap: <span class="intel-big">{suggested_str}</span></h3>'
            f'<ol class="reasoning-steps">'
            f'<li><strong>P95 of your daily spend</strong> = {p95_str} <span class="muted-hint">(only one in 20 days exceeded this)</span></li>'
            f'<li><strong>× 1.5 for headroom</strong> = {format_money(p95_x_15, currency)} <span class="muted-hint">(50% buffer for genuinely busy days)</span></li>'
            f'<li><strong>Suggested cap</strong> = <strong>{suggested_str}</strong> <span class="muted-hint">(rounded up to whole cents)</span></li>'
            f'</ol>'
            f'{ceiling_note}'

            # Profile recommendation
            f'<h3 class="intel-subhead">🎯 Recommended profile: <span class="intel-big">{prof_rec.profile}</span></h3>'
            f'<p class="profile-reason">{prof_rec.reason}</p>'

            # Apply both
            f'<form hx-post="/dashboard/key/{key_id}/apply-suggestion-full" '
            f'hx-target="#intel-section" hx-swap="outerHTML" class="intel-apply-form">'
            f'<input type="hidden" name="cap_cents" value="{sug.suggested_cap_usd_cents}">'
            f'<input type="hidden" name="profile" value="{prof_rec.profile}">'
            f'<button type="submit" class="btn-primary">Apply both — cap {suggested_str} and {prof_rec.profile} profile</button>'
            f'</form>'
            f'<form hx-post="/dashboard/key/{key_id}/apply-suggestion-direct" '
            f'hx-target="#intel-section" hx-swap="outerHTML" style="display:inline">'
            f'<input type="hidden" name="cap_cents" value="{sug.suggested_cap_usd_cents}">'
            f'<button type="submit" class="btn-small">Cap only — keep my profile</button>'
            f'</form> '
            f'<a href="/dashboard/key/{key_id}" class="btn-small">Skip — keep everything</a>'

            f'<p class="muted-hint" style="margin-top:1rem">🔒 Your admin key was used once and immediately wiped. '
            f'Not stored anywhere. You can <a href="https://console.anthropic.com/settings/admin-keys" target="_blank" rel="noopener">delete it from your Anthropic console</a> now.</p>'
            f'</div>'
        )
    except Exception as exc:
        # Never leak the admin key or exception details to the UI.
        try:
            del admin_key
        except (NameError, UnboundLocalError):
            pass
        log.warning("intel_fetch failed: %r", exc)
        return HTMLResponse(
            '<div class="intel-section intel-error">'
            "<p class=\"warn\">⚠ Couldn't fetch usage history. Check that the admin key is valid and "
            "your machine can reach api.anthropic.com, then try again.</p>"
            '</div>',
            status_code=500,
        )


def _build_sparkline(daily_totals: list[int], highlight_value: int) -> str:
    """Inline SVG sparkline. P95 day(s) highlighted in accent colour."""
    if not daily_totals:
        return '<div class="sparkline-empty">No data</div>'
    width, height = 320, 64
    n = len(daily_totals)
    max_v = max(daily_totals) or 1
    bar_w = max(1.0, width / n)
    pad = 4
    inner_h = height - pad * 2
    bars = []
    for i, v in enumerate(daily_totals):
        h = (v / max_v) * inner_h if max_v else 0
        x = i * bar_w
        y = pad + (inner_h - h)
        cls = "spark-bar"
        # Highlight any day whose value equals (or is near) the P95 reference
        if v == highlight_value and v > 0:
            cls += " spark-highlight"
        bars.append(
            f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" '
            f'width="{max(1.0, bar_w - 2):.1f}" height="{max(1.0, h):.1f}" rx="1"/>'
        )
    return (
        f'<svg class="sparkline" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none">'
        f'{"".join(bars)}'
        f'</svg>'
    )


@router.post("/dashboard/key/{key_id}/apply-suggestion-full")
async def apply_suggestion_full(
    request: Request, key_id: uuid.UUID,
    cap_cents: int = Form(...),
    profile: str = Form(...),
) -> HTMLResponse:
    """Apply both the suggested cap AND the recommended profile in one click."""
    if profile not in PROFILES:
        profile = "standard"
    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        ceil = key.absolute_ceiling_usd_cents
        new_cap = min(cap_cents, ceil)
        key.daily_cap_usd_cents = new_cap
        key.profile = profile
        # Respect the profile's default_kill_enabled — monitor defaults to OFF
        key.kill_enabled = PROFILES[profile].default_kill_enabled
        await session.commit()

    currency = settings.display_currency
    kill_note = " Kill switch OFF (monitor mode — alerts only)." if not PROFILES[profile].default_kill_enabled else ""
    return HTMLResponse(
        f'<div class="intel-section intel-result">'
        f'<h2 class="next-steps-heading">✓ Applied</h2>'
        f'<p>Cap set to <strong>{format_money(new_cap, currency)}</strong>, '
        f'profile set to <strong>{profile}</strong>.'
        f'{kill_note}</p>'
        f'<a href="/dashboard/key/{key_id}" class="btn-primary">Open dashboard</a>'
        f'</div>'
    )


@router.post("/dashboard/key/{key_id}/apply-suggestion-direct")
async def apply_suggestion_direct(
    request: Request, key_id: uuid.UUID, cap_cents: int = Form(...)
) -> HTMLResponse:
    """Apply a specific cap value (used by the intel-fetch result panel)."""
    async with get_session() as session:
        key = await _get_key_or_404(key_id, session)
        # Clamp to ceiling
        ceil = key.absolute_ceiling_usd_cents
        new_cap = min(cap_cents, ceil)
        key.daily_cap_usd_cents = new_cap
        await session.commit()

    currency = settings.display_currency
    return HTMLResponse(
        f'<div class="intel-section intel-result">'
        f'<h2 class="next-steps-heading">✓ Cap set to {format_money(new_cap, currency)}</h2>'
        f'<p>You can edit it anytime from the control panel below.</p>'
        f'<a href="/dashboard/key/{key_id}" class="btn-primary">Open dashboard</a>'
        f'</div>'
    )


# ── New key ────────────────────────────────────────────────────────────────────

@router.get("/dashboard/keys/new")
async def new_key_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "key_new.html", {
        "profiles": list(PROFILES.keys()),
        "profiles_obj": PROFILES,
        "currency": settings.display_currency,
    })


@router.post("/dashboard/keys/new")
async def create_key(
    request: Request,
    name: str = Form(...),
    anthropic_key: str = Form(...),
    daily_cap: float = Form(...),
    profile: str = Form("standard"),
    kill_enabled: bool = Form(True),
) -> HTMLResponse:
    if profile not in PROFILES:
        raise HTTPException(status_code=422, detail="Invalid profile")
    if not anthropic_key.startswith("sk-ant-"):
        raise HTTPException(status_code=422, detail="Key must start with sk-ant-")

    currency = settings.display_currency
    cap_cents = from_major_units(daily_cap, currency)
    if cap_cents < 1:
        raise HTTPException(status_code=422, detail="Cap must be at least 1 cent")

    token = _make_tq_token()
    token_hash = _hash_token(token)
    encrypted_key = _encrypt_anthropic_key(anthropic_key)

    async with get_session() as session:
        key = ApiKey(
            name=name,
            tq_token_hash=token_hash,
            anthropic_key_encrypted=encrypted_key,
            profile=profile,
            daily_cap_usd_cents=cap_cents,
            kill_enabled=kill_enabled,
            user_id=uuid.uuid4(),  # no multi-user on localhost; use a throwaway UUID
        )
        session.add(key)
        await session.commit()
        key_id = str(key.id)

    return templates.TemplateResponse(request, "key_rotated.html", {
        "token": token,
        "key_id": key_id,
        "key_name": name,
        "is_new": True,
        "default_shell": _default_shell(request),
    })
