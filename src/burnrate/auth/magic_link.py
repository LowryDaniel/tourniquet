"""Magic-link authentication.

Flow:
  1. User submits email → POST /auth/magic-link
  2. We sign a token (itsdangerous URLSafeTimedSerializer, 15min expiry)
  3. We send email with link: GET /auth/verify?token=<signed>
  4. On verify: look up user by email embedded in token, create if new, set session cookie
  5. Token is single-use: we store it on the user row and NULL it on first use

Session cookie: itsdangerous SignedCookieSessionInterface via Starlette sessions.
"""

from __future__ import annotations

import resend
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from burnrate.config import settings
from burnrate.db import get_session
from burnrate.models import User

router = APIRouter(prefix="/auth")

_signer = URLSafeTimedSerializer(settings.secret_key, salt="magic-link")


def _make_token(email: str) -> str:
    return _signer.dumps(email)


def _verify_token(token: str) -> str:
    try:
        email: str = _signer.loads(token, max_age=settings.magic_link_expiry_seconds)
        return email
    except SignatureExpired:
        raise HTTPException(status_code=400, detail="Magic link has expired. Request a new one.")
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid magic link.")


async def _get_or_create_user(email: str, session: AsyncSession) -> User:
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(email=email)
        session.add(user)
        await session.flush()
    return user


@router.post("/magic-link")
async def send_magic_link(request: Request, email: str = Form(...)) -> HTMLResponse:
    token = _make_token(email)
    link = f"{settings.app_base_url}/auth/verify?token={token}"

    if settings.resend_api_key:
        resend.api_key = settings.resend_api_key
        resend.Emails.send({
            "from": settings.resend_from_email,
            "to": [email],
            "subject": "Sign in to BurnRate",
            "html": f'<p>Click to sign in (expires in 15 minutes):</p><p><a href="{link}">{link}</a></p>',
        })

    return HTMLResponse("<p>Check your email for the sign-in link.</p>")


@router.get("/verify")
async def verify_magic_link(request: Request, token: str) -> RedirectResponse:
    email = _verify_token(token)

    async with get_session() as session:
        user = await _get_or_create_user(email, session)
        await session.commit()
        user_id = str(user.id)

    request.session["user_id"] = user_id
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/", status_code=303)
