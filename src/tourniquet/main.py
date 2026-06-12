"""FastAPI application factory."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import sentry_sdk
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from tourniquet.alerts.telegram_callbacks import router as telegram_callback_router
from tourniquet.auth.magic_link import router as auth_router
from tourniquet.config import settings
from tourniquet.dashboard.routes import router as dashboard_router
from tourniquet.proxy.router import router as proxy_router
from tourniquet.routes.admin import router as admin_router

_ASSETS_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    if settings.sentry_dsn:
        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.app_env)

    # Bring the schema to HEAD before serving traffic.
    # Idempotent: already-applied migrations are a fast no-op.
    from tourniquet.migrate import upgrade_to_head

    upgrade_to_head(settings.database_url)

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
        # GIT_SHA is baked in at image build time (see Dockerfile ARG); it lets
        # the /ship gate prove WHICH commit is live, not just that the app is up.
        return {
            "status": "ok",
            "version": "0.1.0",
            "commit": os.getenv("GIT_SHA", "unknown"),
        }

    return app


app = create_app()
