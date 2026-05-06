"""FastAPI application factory."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import sentry_sdk
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from burnrate.auth.magic_link import router as auth_router
from burnrate.config import settings
from burnrate.dashboard.routes import router as dashboard_router
from burnrate.proxy.router import router as proxy_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    if settings.sentry_dsn:
        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.app_env)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="BurnRate",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
    app.mount("/static", StaticFiles(directory="static"), name="static")

    app.include_router(proxy_router)
    app.include_router(auth_router)
    app.include_router(dashboard_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    return app


app = create_app()
