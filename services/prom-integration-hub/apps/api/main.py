"""FastAPI application factory and entrypoint (uvicorn target: apps.api.main:app)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from apps.api.error_handlers import register_error_handlers
from apps.api.routers import (
    account_linking,
    customers,
    identities,
    installations,
    instances,
    line_webhook,
    prom_webhook,
    work_items,
    workspaces,
)
from src.adapters.redis.client import get_redis_client
from src.infrastructure.configuration.settings import get_settings
from src.infrastructure.database.engine import get_engine
from src.infrastructure.observability.logging import (
    configure_logging,
    reset_correlation_id,
    set_correlation_id,
)


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="Prom Integration Hub", version="0.1.0")

    @app.middleware("http")
    async def _correlation_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Binds a correlation id (from X-Correlation-Id, or a new uuid4) to every request, for
        the duration of that request only — propagated into every audit entry and log line the
        request produces, and echoed back in the response header."""
        correlation_id = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
        set_correlation_id(correlation_id)
        try:
            response = await call_next(request)
        finally:
            reset_correlation_id()
        response.headers["X-Correlation-Id"] = correlation_id
        return response

    register_error_handlers(app)
    app.include_router(customers.router)
    app.include_router(instances.router)
    app.include_router(workspaces.router)
    app.include_router(installations.router)
    app.include_router(account_linking.router)
    app.include_router(identities.router)
    app.include_router(work_items.router)
    app.include_router(line_webhook.router)
    app.include_router(prom_webhook.router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness probe — process is up. No dependency checks."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Readiness probe — checks database and Redis connectivity."""
        failures: dict[str, str] = {}

        try:
            engine = get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - readiness must report any dependency failure
            failures["database"] = str(exc)

        try:
            redis_client = get_redis_client()
            await redis_client.ping()
        except Exception as exc:  # noqa: BLE001 - readiness must report any dependency failure
            failures["redis"] = str(exc)

        body: dict[str, Any] = {"status": "ok" if not failures else "unavailable"}
        if failures:
            body["failures"] = failures
        return JSONResponse(content=body, status_code=200 if not failures else 503)

    return app


app = create_app()
