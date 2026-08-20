"""Maps typed DomainError subclasses to HTTP responses. Registered once in create_app()."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.infrastructure.observability.logging import get_correlation_id
from src.shared.errors import DomainError

# error_code -> HTTP status. Anything not listed here falls back to 400 (a domain/validation
# rejection we didn't anticipate a more specific status for, still not a 500 since it's a typed,
# expected error rather than a bug).
_ERROR_CODE_STATUS: dict[str, int] = {
    "NOT_FOUND": 404,
    "CHANNEL_CREDENTIAL_INVALID": 422,
    "LINKING_CODE_INVALID": 422,
    "LINKING_CODE_EXPIRED": 422,
    "RATE_LIMITED": 429,
    "INTEGRATION_INACTIVE": 409,
    "WORKSPACE_NOT_ALLOWED": 403,
    "IDENTITY_NOT_LINKED": 404,
    "PROM_API_UNAVAILABLE": 502,
    "PROM_PERMISSION_DENIED": 403,
    "TENANT_ISOLATION_VIOLATION": 403,
    "IDENTITY_ALREADY_LINKED": 409,
    "WORK_ITEM_NOT_FOUND": 404,
    "PROM_REQUEST_INVALID": 422,
    "IDEMPOTENCY_KEY_CONFLICT": 409,
    "CHANNEL_DELIVERY_FAILED": 502,
    "INSTALLATION_REVOKED": 409,
}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        status_code = _ERROR_CODE_STATUS.get(exc.error_code, 400)
        return JSONResponse(
            status_code=status_code,
            content={
                "error_code": exc.error_code,
                "message": exc.message,
                "correlation_id": get_correlation_id(),
            },
        )
