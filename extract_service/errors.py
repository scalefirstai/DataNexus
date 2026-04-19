"""Error taxonomy (§7.1) and HTTP mapping."""

from __future__ import annotations

from typing import Any

from .models import ErrorCode


class ExtractError(Exception):
    http_status: int = 500
    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(ExtractError):
    http_status = 400
    code = ErrorCode.VALIDATION_ERROR


class EntitlementDenied(ExtractError):
    http_status = 403
    code = ErrorCode.ENTITLEMENT_DENIED


class Unauthorized(ExtractError):
    http_status = 401
    code = ErrorCode.VALIDATION_ERROR


class NotFoundError(ExtractError):
    http_status = 404
    code = ErrorCode.VALIDATION_ERROR


class IdempotencyConflict(ExtractError):
    http_status = 409
    code = ErrorCode.VALIDATION_ERROR


class RateLimited(ExtractError):
    http_status = 429
    code = ErrorCode.RATE_LIMITED

    def __init__(
        self, message: str, retry_after: int, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message, details)
        self.retry_after = retry_after
