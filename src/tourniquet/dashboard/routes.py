"""Dashboard routes — HTMX/Jinja2, not a public API.

All routes require a valid session cookie (user_id).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import date

import bcrypt
from cryptography.fernet import Fernet
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourniquet.billing.caps import get_today_spend
from tourniquet.billing.profiles import PROFILES
from tourniquet.config import settings
from tourniquet.db import get_session
from tourniquet.models import ApiKey, UsageEvent, User

router = APIRouter()
templates = Jinja2Templates(directory="templates")

_fernet = Fernet(settings.fernet_key.encode())


def _require_user_id(request: Request) -> uuid.UUID:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return uuid.UUID(uid)


def _make_tq_token() -> str:
    """Generate a random tq_* token (shown once, then hashed)."""
    return "tq_" + secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    return bcrypt.hashpw(token.encode(), bcrypt.gensalt()).decode()


def _encrypt_anthropic_key(raw_key: str) -> str:
    return _fernet.encrypt(raw_key.encode()).decode()


@router.get("/")
async def landing(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "landing.html")


@router.get("/login")
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html")


@router.get("/dashboard")
async def dashboard(request: Request) -> HTMLResponse:
    user_id = _require_user_id(request)
    today = date.today()

    async with get_session() as session:
        result = await session.execute(select(ApiKey).where(ApiKey.user_id == user_id))
        keys = result.scalars().all()

        key_summaries = []
        for k in keys:
            spent = await get_today_spend(k.id, today, session)
            key_summaries.append({
                "id": str(k.id),
                "name": k.name,
                "profile": k.profile,
                "daily_cap_pence": k.daily_cap_pence,
                "spent_pence": spent,
                "pct": int(spent / k.daily_cap_pence * 100) if k.daily_cap_pence else 0,
                "kill_enabled": k.kill_enabled,
            })

    return templates.TemplateResponse(request, "dashboard.html", {
        "keys": key_summaries,
        "profiles": list(PROFILES.keys()),
    })


@router.post("/dashboard/keys")
async def create_key(
    request: Request,
    name: str = Form(...),
    anthropic_key: str = Form(...),
    profile: str = Form("hobby"),
    daily_cap_pence: int = Form(500),
    kill_enabled: bool = Form(True),
    alert_email: str = Form(""),
) -> HTMLResponse:
    user_id = _require_user_id(request)

    if profile not in PROFILES:
        raise HTTPException(status_code=422, detail="Invalid profile.")
    if not anthropic_key.startswith("sk-ant-"):
        raise HTTPException(status_code=422, detail="Key must start with sk-ant-")
    if daily_cap_pence < 1:
        raise HTTPException(status_code=422, detail="Cap must be at least 1 pence.")

    token = _make_tq_token()
    token_hash = _hash_token(token)
    encrypted_key = _encrypt_anthropic_key(anthropic_key)

    async with get_session() as session:
        key = ApiKey(
            user_id=user_id,
            name=name,
            tq_token_hash=token_hash,
            anthropic_key_encrypted=encrypted_key,
            profile=profile,
            daily_cap_pence=daily_cap_pence,
            kill_enabled=kill_enabled,
            alert_email=alert_email or None,
        )
        session.add(key)
        await session.commit()
        key_id = str(key.id)

    return templates.TemplateResponse(request, "key_created.html", {
        "token": token,
        "key_id": key_id,
        "name": name,
        "app_base_url": settings.app_base_url,
    })


@router.delete("/dashboard/keys/{key_id}")
async def delete_key(request: Request, key_id: uuid.UUID) -> RedirectResponse:
    user_id = _require_user_id(request)

    async with get_session() as session:
        result = await session.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id)
        )
        key = result.scalar_one_or_none()
        if not key:
            raise HTTPException(status_code=404)
        await session.delete(key)
        await session.commit()

    return RedirectResponse("/dashboard", status_code=303)


@router.patch("/dashboard/keys/{key_id}")
async def update_key(
    request: Request,
    key_id: uuid.UUID,
    profile: str = Form(None),
    daily_cap_pence: int = Form(None),
    kill_enabled: bool = Form(None),
    alert_email: str = Form(None),
) -> RedirectResponse:
    user_id = _require_user_id(request)

    async with get_session() as session:
        result = await session.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id)
        )
        key = result.scalar_one_or_none()
        if not key:
            raise HTTPException(status_code=404)

        if profile is not None:
            if profile not in PROFILES:
                raise HTTPException(status_code=422, detail="Invalid profile.")
            key.profile = profile
        if daily_cap_pence is not None:
            key.daily_cap_pence = daily_cap_pence
        if kill_enabled is not None:
            key.kill_enabled = kill_enabled
        if alert_email is not None:
            key.alert_email = alert_email or None

        await session.commit()

    return RedirectResponse("/dashboard", status_code=303)


@router.get("/dashboard/keys/{key_id}/usage")
async def key_usage(request: Request, key_id: uuid.UUID) -> HTMLResponse:
    user_id = _require_user_id(request)

    async with get_session() as session:
        result = await session.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id)
        )
        key = result.scalar_one_or_none()
        if not key:
            raise HTTPException(status_code=404)

        events_result = await session.execute(
            select(UsageEvent)
            .where(UsageEvent.api_key_id == key_id)
            .order_by(desc(UsageEvent.created_at))
            .limit(50)
        )
        events = events_result.scalars().all()

    return templates.TemplateResponse(request, "usage.html", {
        "key": key,
        "events": events,
    })
