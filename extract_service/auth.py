"""Auth + entitlements + rate limiting (§6, §8.3)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from fastapi import Header, HTTPException, Request, status

from .config import get_settings
from .errors import EntitlementDenied, RateLimited, Unauthorized
from .storage import Storage, get_storage


@dataclass
class Principal:
    app_id: str
    cert_fingerprint: str | None
    token: str


def _bearer(auth: str | None) -> str | None:
    if not auth:
        return None
    if not auth.lower().startswith("bearer "):
        return None
    return auth.split(" ", 1)[1].strip()


def authenticate(
    request: Request,
    authorization: str | None = Header(default=None),
    x_mtls_fingerprint: str | None = Header(default=None),
) -> Principal:
    """Mock OAuth2 client-credentials bearer + optional mTLS fingerprint check.

    Production replaces this with an identity-provider token introspection
    plus mTLS certificate validation at the ingress.
    """
    token = _bearer(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid Authorization header",
        )
    storage = get_storage()
    app = storage.lookup_app_by_token(token)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unknown token"
        )
    if app.get("cert_fingerprint") and x_mtls_fingerprint:
        if app["cert_fingerprint"] != x_mtls_fingerprint:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="mTLS fingerprint mismatch",
            )
    principal = Principal(
        app_id=app["app_id"],
        cert_fingerprint=x_mtls_fingerprint,
        token=token,
    )
    request.state.principal = principal
    return principal


def check_entitlements(
    storage: Storage, app_id: str, requested: list[str] | None
) -> list[str]:
    entitled = storage.entitled_funds(app_id)
    if not requested:
        if not entitled:
            raise EntitlementDenied(
                f"app {app_id} has no fund entitlements and no fund_scope provided"
            )
        return sorted(entitled)
    missing = [f for f in requested if f not in entitled]
    if missing:
        raise EntitlementDenied(
            f"app {app_id} not entitled to funds: {missing}",
            details={"missing_funds": missing},
        )
    return requested


class RateLimiter:
    """Sliding-window rate limits per app_id, plus concurrent extract cap."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def _check_window(
        self, app_id: str, kind: str, limit_per_minute: int
    ) -> tuple[int, int, int]:
        now = time.time()
        window_start = now - 60
        self.storage.prune_rl_events(window_start - 3600)
        count = self.storage.count_rl_events(app_id, kind, window_start)
        remaining = max(0, limit_per_minute - count - 1)
        reset = int(now) + 60
        return count, remaining, reset

    def check_request(self, app_id: str) -> dict[str, Any]:
        settings = get_settings()
        count, remaining, reset = self._check_window(
            app_id, "request", settings.rate_limit_requests_per_minute
        )
        if count >= settings.rate_limit_requests_per_minute:
            raise RateLimited(
                "request rate limit exceeded", retry_after=60,
                details={"limit_per_minute": settings.rate_limit_requests_per_minute},
            )
        self.storage.record_rl_event(app_id, "request", time.time())
        return {
            "X-RateLimit-Limit": str(settings.rate_limit_requests_per_minute),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset),
        }

    def check_presign(self, app_id: str) -> None:
        settings = get_settings()
        count, _, _ = self._check_window(
            app_id, "presign", settings.rate_limit_presign_per_minute
        )
        if count >= settings.rate_limit_presign_per_minute:
            raise RateLimited(
                "presigned URL rate limit exceeded",
                retry_after=60,
            )
        self.storage.record_rl_event(app_id, "presign", time.time())

    def check_concurrent(self, app_id: str) -> None:
        settings = get_settings()
        active = self.storage.count_active_extracts(app_id)
        if active >= settings.rate_limit_concurrent_extracts:
            raise RateLimited(
                "concurrent active extract limit exceeded",
                retry_after=30,
                details={
                    "limit": settings.rate_limit_concurrent_extracts,
                    "active": active,
                },
            )


def get_rate_limiter() -> RateLimiter:
    return RateLimiter(get_storage())
