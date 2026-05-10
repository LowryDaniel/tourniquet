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

import hashlib
import hmac

import resend
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourniquet.config import settings
from tourniquet.db import get_session
from tourniquet.models import User


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


router = APIRouter(prefix="/auth")

_signer = URLSafeTimedSerializer(settings.secret_key, salt="magic-link")


def _make_token(email: str) -> str:
    return _signer.dumps(email)


def _verify_token(token: str) -> str:
    try:
        email: str = _signer.loads(token, max_age=settings.magic_link_expiry_seconds)
        return email
    except SignatureExpired as exc:
        raise HTTPException(
            status_code=400,
            detail="Magic link has expired. Request a new one.",
        ) from exc
    except BadSignature as exc:
        raise HTTPException(status_code=400, detail="Invalid magic link.") from exc


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

    # Store the token hash on the user row so verify can consume it exactly once.
    async with get_session() as session:
        user = await _get_or_create_user(email, session)
        user.magic_link_token = _token_hash(token)
        await session.commit()

    if settings.resend_api_key:
        resend.api_key = settings.resend_api_key
        resend.Emails.send(
            {
                "from": settings.resend_from_email,
                "to": [email],
                "subject": "Sign in to Tourniquet",
                "html": (
                    f"<p>Click to sign in (expires in 15 minutes):</p>"
                    f'<p><a href="{link}">{link}</a></p>'
                ),
            }
        )

    return HTMLResponse("<p>Check your email for the sign-in link.</p>")


@router.get("/verify")
async def verify_magic_link(request: Request, token: str) -> RedirectResponse:
    email = _verify_token(token)
    expected_hash = _token_hash(token)

    async with get_session() as session:
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if user is None or user.magic_link_token is None:
            raise HTTPException(
                status_code=400,
                detail="Magic link has already been used or was never issued.",
            )

        if not hmac.compare_digest(user.magic_link_token, expected_hash):
            raise HTTPException(
                status_code=400,
                detail=(
                    "This sign-in link is no longer valid. "
                    "If you requested a new link, use the most recent email."
                ),
            )

        # Consume the token — NULL it out so it can't be replayed.
        user.magic_link_token = None
        await session.commit()
        user_id = str(user.id)

    request.session["user_id"] = user_id
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/", status_code=303)
