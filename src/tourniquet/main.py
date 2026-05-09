"""FastAPI application factory."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import sentry_sdk
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from tourniquet.auth.magic_link import router as auth_router
from tourniquet.config import settings
from tourniquet.dashboard.routes import router as dashboard_router
from tourniquet.proxy.router import router as proxy_router
from tourniquet.routes.admin import router as admin_router
from tourniquet.alerts.telegram_callbacks import router as telegram_callback_router


_ASSETS_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    if settings.sentry_dsn:
        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.app_env)

    # Auto-create schema on first run — users don't need a separate migration step.
    # Idempotent: re-run is a no-op if tables already exist.
    from tourniquet.db import engine
    from tourniquet.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Auto-start Telegram polling so inline buttons work in-app without a webhook
    from tourniquet.alerts.telegram_poller import poller as telegram_poller
    await telegram_poller.start()

    # Auto-start Slack Socket Mode so inline buttons work in-app without an HTTPS callback URL
    from tourniquet.alerts.slack_socket import socket_client as slack_socket_client
    await slack_socket_client.start()

    try:
        yield
    finally:
        await telegram_poller.stop()
        await slack_socket_client.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Tourniquet",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
    app.mount("/static", StaticFiles(directory=str(_ASSETS_DIR / "static")), name="static")

    app.include_router(proxy_router)
    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(admin_router)
    app.include_router(telegram_callback_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    return app


app = create_app()
