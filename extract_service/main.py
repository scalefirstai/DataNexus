"""FastAPI application wiring §3 REST API endpoints + §10 versioning."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse

from .auth import Principal, authenticate, check_entitlements, get_rate_limiter
from .config import get_settings
from .domains import REGISTRY, get_domain
from .errors import (
    EntitlementDenied,
    ExtractError,
    IdempotencyConflict,
    NotFoundError,
    RateLimited,
    Unauthorized,
    ValidationError,
)
from .event_bus import get_event_bus
from .logging_config import configure_logging, set_log_context
from .metrics import get_metrics
from .models import (
    AcceptResponse,
    ApiError,
    CancelResponse,
    CreateExtractRequest,
    ErrorCode,
    ExtractLinks,
    ExtractStatus,
    FileEntry,
    FilesResponse,
    ListExtractsEntry,
    ListExtractsResponse,
    ManifestLink,
    NotificationMode,
    Priority,
    ProgressInfo,
    ResultInfo,
    StatusResponse,
    TERMINAL_STATUSES,
    LineageInfo,
    ErrorDetail,
    utcnow,
)
from .object_store import get_object_store
from .storage import get_storage
from .webhooks import get_retention_sweeper, get_webhook_delivery
from .worker import Worker

log = logging.getLogger("extract.api")


# ---------------------------------------------------------------------------
# Lifespan: start worker, webhook delivery, retention sweeper
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(service="extract-api")
    storage = get_storage()
    object_store = get_object_store()
    event_bus = get_event_bus()
    webhook_delivery = get_webhook_delivery()
    worker = Worker(storage, object_store, event_bus, webhook_delivery)
    sweeper = get_retention_sweeper(storage, object_store, event_bus)

    await webhook_delivery.start()
    await worker.start()
    await sweeper.start()

    _seed_demo_tenants(storage)

    app.state.storage = storage
    app.state.object_store = object_store
    app.state.event_bus = event_bus
    app.state.worker = worker
    app.state.webhook_delivery = webhook_delivery
    app.state.sweeper = sweeper

    try:
        yield
    finally:
        await sweeper.stop()
        await worker.stop()
        await webhook_delivery.stop()


def _seed_demo_tenants(storage) -> None:
    fes_funds = [f"fund_{chr(ord('A') + i)}" for i in range(26)] + [
        f"fund_{i:03d}" for i in range(100)
    ]
    if not storage.get_app("fes-plus-plus"):
        storage.register_app(
            app_id="fes-plus-plus",
            token="demo-token-fes",
            webhook_secret="fes-webhook-secret",
            cert_fingerprint="demo-cert-fes",
            fund_ids=fes_funds,
        )
    else:
        with storage.tx() as conn:
            for f in fes_funds:
                conn.execute(
                    "INSERT OR IGNORE INTO entitlements(app_id, fund_id) VALUES (?, ?)",
                    ("fes-plus-plus", f),
                )
    if not storage.get_app("calc-engine"):
        storage.register_app(
            app_id="calc-engine",
            token="demo-token-calc",
            webhook_secret="calc-webhook-secret",
            fund_ids=["fund_A", "fund_B"],
        )


app = FastAPI(
    title="Extract API",
    version="1.0.0",
    description="Bulk extract & event-driven distribution — reference implementation",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@app.exception_handler(ExtractError)
async def _extract_error_handler(request: Request, exc: ExtractError) -> JSONResponse:
    body = ApiError(
        code=exc.code, message=exc.message, details=exc.details
    ).model_dump(mode="json")
    headers: dict[str, str] = {}
    if isinstance(exc, RateLimited):
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(status_code=exc.http_status, content=body, headers=headers)


@app.exception_handler(Exception)
async def _fallback_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled")
    return JSONResponse(
        status_code=500,
        content=ApiError(
            code=ErrorCode.INTERNAL_ERROR, message=str(exc)
        ).model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# Middleware: correlation_id propagation + audit
# ---------------------------------------------------------------------------


@app.middleware("http")
async def context_middleware(request: Request, call_next):
    correlation_id = (
        request.headers.get("X-Correlation-Id")
        or request.headers.get("X-Request-Id")
        or _gen_correlation_id()
    )
    set_log_context(correlation_id=correlation_id)
    response = await call_next(request)
    response.headers["X-Correlation-Id"] = correlation_id
    return response


def _gen_correlation_id() -> str:
    return "corr-" + secrets.token_hex(6)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request_fingerprint(req: CreateExtractRequest) -> str:
    dumped = req.model_dump(mode="json", exclude={"idempotency_key"})
    return hashlib.sha256(
        json.dumps(dumped, sort_keys=True, default=str).encode()
    ).hexdigest()


def _make_extract_id(now: datetime) -> str:
    suffix = secrets.token_hex(2)
    return f"ext_{now.strftime('%Y%m%d_%H%M%S')}_{suffix}"


def _validate_request(req: CreateExtractRequest) -> None:
    settings = get_settings()
    if get_domain(req.domain) is None:
        raise ValidationError(f"unknown domain: {req.domain}")
    years = (req.period.end - req.period.start).days / 365.25
    if years > settings.max_period_years:
        raise ValidationError(
            f"period exceeds max {settings.max_period_years} years"
        )
    if req.fund_scope and len(req.fund_scope) > settings.max_fund_scope:
        raise ValidationError(
            f"fund_scope exceeds max of {settings.max_fund_scope}"
        )


def _status_response_from_row(row: dict[str, Any]) -> StatusResponse:
    state = row.get("state") or {}
    status_ = ExtractStatus(row["status"])
    progress = None
    if state.get("progress"):
        progress = ProgressInfo(**state["progress"])

    result = None
    if status_ in (ExtractStatus.COMPLETED, ExtractStatus.PARTIAL) and state.get("files"):
        result = ResultInfo(
            location=f"/api/v1/extracts/{row['extract_id']}/files",
            file_count=len(state["files"]),
            total_bytes=state.get("total_bytes", 0),
            total_rows=state.get("total_rows", 0),
            checksum=state.get("manifest_checksum", ""),
            schema_version=state.get("schema_version", ""),
            expires_at=_parse_iso(row.get("expires_at")) or utcnow(),
        )

    lineage = None
    if row.get("run_id"):
        request = row["request"]
        domain = row["domain"]
        lineage = LineageInfo(
            sources=list(get_domain(domain).sources) if get_domain(domain) else [],
            as_of=request.get("as_of"),
            run_id=row["run_id"],
        )

    err = None
    if state.get("error"):
        err = ErrorDetail(**state["error"])

    completed_at = _parse_iso(row.get("completed_at"))
    created_at = _parse_iso(row["created_at"])
    duration = None
    if completed_at and created_at:
        duration = int((completed_at - created_at).total_seconds())

    return StatusResponse(
        extract_id=row["extract_id"],
        status=status_,
        progress=progress,
        created_at=created_at,  # type: ignore[arg-type]
        updated_at=_parse_iso(row.get("updated_at")),
        completed_at=completed_at,
        cancelled_at=_parse_iso(row.get("cancelled_at")),
        duration_seconds=duration,
        estimated_completion=None,
        result=result,
        lineage=lineage,
        error=err,
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# v1 router — production API
# ---------------------------------------------------------------------------


def _register_routes(app: FastAPI, prefix: str, sandbox: bool) -> None:
    @app.post(
        f"{prefix}/extracts",
        response_model=AcceptResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses={
            200: {
                "description": "Idempotent duplicate — existing extract returned",
                "model": AcceptResponse,
            },
            400: {"model": ApiError},
            401: {"model": ApiError},
            403: {"model": ApiError},
            409: {"model": ApiError},
            429: {"model": ApiError},
        },
    )
    async def create_extract(
        body: CreateExtractRequest,
        response: Response,
        request: Request,
        principal: Principal = Depends(authenticate),
    ):
        if body.requester.app_id != principal.app_id:
            raise ValidationError(
                "requester.app_id must match the authenticated application"
            )
        rl = get_rate_limiter()
        headers = rl.check_request(principal.app_id)
        for k, v in headers.items():
            response.headers[k] = v

        _validate_request(body)
        storage = get_storage()

        fingerprint = _request_fingerprint(body)
        existing = storage.lookup_idempotency(
            principal.app_id, body.idempotency_key
        )
        if existing:
            if existing["request_fingerprint"] != fingerprint:
                raise IdempotencyConflict(
                    "idempotency key reused with different parameters",
                    details={"extract_id": existing["extract_id"]},
                )
            row = storage.get_extract(existing["extract_id"])
            response.status_code = status.HTTP_200_OK
            response.headers["Location"] = (
                f"{prefix}/extracts/{existing['extract_id']}"
            )
            response.headers["Retry-After"] = "30"
            return AcceptResponse(
                extract_id=existing["extract_id"],
                status=ExtractStatus(row["status"]),
                created_at=_parse_iso(row["created_at"]),  # type: ignore[arg-type]
                links=ExtractLinks(
                    self=f"{prefix}/extracts/{existing['extract_id']}",
                    cancel=f"{prefix}/extracts/{existing['extract_id']}/cancel",
                ),
                estimated_completion=None,
                retry_after_seconds=30,
            )

        resolved_scope = check_entitlements(
            storage, principal.app_id, body.fund_scope
        )
        rl.check_concurrent(principal.app_id)

        now = utcnow()
        extract_id = _make_extract_id(now)
        request_json = body.model_dump(mode="json")
        request_json["fund_scope"] = resolved_scope
        storage.create_extract(
            extract_id=extract_id,
            idempotency_key=body.idempotency_key,
            app_id=principal.app_id,
            domain=body.domain,
            priority=body.priority.value,
            request_json=request_json,
            state_json={
                "progress": {
                    "funds_completed": 0,
                    "funds_total": len(resolved_scope),
                    "percent": 0,
                    "current_fund": None,
                }
            },
        )
        storage.record_idempotency(
            principal.app_id, body.idempotency_key, extract_id, fingerprint
        )
        storage.audit(
            principal.app_id,
            "extract.create",
            extract_id,
            "success",
            {"domain": body.domain, "fund_scope": resolved_scope},
        )
        get_metrics().inc(
            "extract.requests.total",
            labels={"domain": body.domain, "status": "ACCEPTED", "app_id": principal.app_id},
        )

        worker: Worker = app.state.worker
        await worker.enqueue(extract_id, body.priority)

        estimated = now + _estimate_duration(resolved_scope, body.domain)
        response.headers["Location"] = f"{prefix}/extracts/{extract_id}"
        response.headers["Retry-After"] = "30"
        return AcceptResponse(
            extract_id=extract_id,
            status=ExtractStatus.ACCEPTED,
            created_at=now,
            links=ExtractLinks(
                self=f"{prefix}/extracts/{extract_id}",
                cancel=f"{prefix}/extracts/{extract_id}/cancel",
            ),
            estimated_completion=estimated,
            retry_after_seconds=30,
        )

    @app.get(
        f"{prefix}/extracts/{{extract_id}}",
        response_model=StatusResponse,
        responses={401: {"model": ApiError}, 404: {"model": ApiError}},
    )
    async def get_extract_status(
        extract_id: str,
        response: Response,
        principal: Principal = Depends(authenticate),
    ):
        storage = get_storage()
        rl = get_rate_limiter()
        for k, v in rl.check_request(principal.app_id).items():
            response.headers[k] = v
        row = storage.get_extract(extract_id)
        if not row or row["app_id"] != principal.app_id:
            raise NotFoundError("extract not found")
        return _status_response_from_row(row)

    @app.get(
        f"{prefix}/extracts/{{extract_id}}/files",
        response_model=FilesResponse,
        responses={
            401: {"model": ApiError},
            403: {"model": ApiError},
            404: {"model": ApiError},
            409: {"model": ApiError},
        },
    )
    async def list_files(
        extract_id: str,
        request: Request,
        response: Response,
        principal: Principal = Depends(authenticate),
    ):
        storage = get_storage()
        object_store = get_object_store()
        rl = get_rate_limiter()
        for k, v in rl.check_request(principal.app_id).items():
            response.headers[k] = v

        row = storage.get_extract(extract_id)
        if not row or row["app_id"] != principal.app_id:
            raise NotFoundError("extract not found")
        if row["status"] == ExtractStatus.EXPIRED.value:
            raise HTTPException(
                status_code=410,
                detail={
                    "code": ErrorCode.VALIDATION_ERROR.value,
                    "message": (
                        "extract has expired per retention policy; files are "
                        "no longer available. Re-submit a fresh extract request "
                        "if the data is still required."
                    ),
                },
            )
        if row["status"] not in (
            ExtractStatus.COMPLETED.value,
            ExtractStatus.PARTIAL.value,
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": ErrorCode.VALIDATION_ERROR.value,
                    "message": f"extract is {row['status']}; files unavailable",
                },
            )
        entitled_funds = storage.entitled_funds(principal.app_id)
        state = row["state"]
        files = []
        base_url = str(request.base_url).rstrip("/")
        for f in state.get("files", []):
            if f["partition_key"] not in entitled_funds:
                continue
            rl.check_presign(principal.app_id)
            token, expires = object_store.sign(
                extract_id, row["domain"], f["name"], principal.app_id
            )
            get_metrics().inc(
                "extract.storage.presigned_urls_issued",
                labels={"app_id": principal.app_id},
            )
            files.append(
                FileEntry(
                    file_id=f"file_{f['partition_key']}",
                    partition_key=f["partition_key"],
                    format=f["format"],
                    compression=f["compression"],
                    size_bytes=f["size_bytes"],
                    row_count=f["row_count"],
                    checksum=f["checksum"],
                    download_url=(
                        f"{base_url}{prefix}/extracts/{extract_id}/download/"
                        f"{f['name']}?token={token}"
                    ),
                    url_expires_at=expires,
                )
            )
        rl.check_presign(principal.app_id)
        manifest_token, manifest_expires = object_store.sign(
            extract_id, row["domain"], "manifest.json", principal.app_id
        )
        storage.audit(
            principal.app_id,
            "extract.files.list",
            extract_id,
            "success",
            {"files_returned": len(files)},
        )
        return FilesResponse(
            extract_id=extract_id,
            files=files,
            manifest=ManifestLink(
                download_url=(
                    f"{base_url}{prefix}/extracts/{extract_id}/download/manifest.json"
                    f"?token={manifest_token}"
                ),
                url_expires_at=manifest_expires,
            ),
        )

    @app.get(f"{prefix}/extracts/{{extract_id}}/download/{{filename}}")
    async def download_file(
        extract_id: str,
        filename: str,
        token: str = Query(...),
    ):
        storage = get_storage()
        object_store = get_object_store()
        row = storage.get_extract(extract_id)
        if not row:
            raise NotFoundError("extract not found")
        ok, app_id, err = object_store.verify(extract_id, row["domain"], filename, token)
        if not ok:
            raise HTTPException(status_code=403, detail=err or "invalid token")
        path = object_store.file_path(extract_id, row["domain"], filename)
        if not path:
            raise NotFoundError("file not found")
        storage.audit(
            app_id,
            "file.download",
            extract_id,
            "success",
            {"filename": filename, "size_bytes": path.stat().st_size},
        )
        return FileResponse(path, filename=filename)

    @app.post(
        f"{prefix}/extracts/{{extract_id}}/cancel",
        response_model=CancelResponse,
        responses={401: {"model": ApiError}, 404: {"model": ApiError}},
    )
    async def cancel_extract(
        extract_id: str,
        response: Response,
        principal: Principal = Depends(authenticate),
    ):
        storage = get_storage()
        rl = get_rate_limiter()
        for k, v in rl.check_request(principal.app_id).items():
            response.headers[k] = v
        row = storage.get_extract(extract_id)
        if not row or row["app_id"] != principal.app_id:
            raise NotFoundError("extract not found")
        current = ExtractStatus(row["status"])
        if current in TERMINAL_STATUSES:
            # idempotent — return 200 with existing cancelled_at if set
            return CancelResponse(
                extract_id=extract_id,
                status=current,
                cancelled_at=_parse_iso(row.get("cancelled_at")) or utcnow(),
                cancelled_by=principal.app_id,
            )
        worker: Worker = app.state.worker
        worker.cancel(extract_id)
        now = utcnow()
        storage.update_extract(
            extract_id, status=ExtractStatus.CANCELLED, cancelled_at=now
        )
        storage.audit(
            principal.app_id, "extract.cancel", extract_id, "success"
        )
        return CancelResponse(
            extract_id=extract_id,
            status=ExtractStatus.CANCELLED,
            cancelled_at=now,
            cancelled_by=principal.app_id,
        )

    @app.get(
        f"{prefix}/extracts",
        response_model=ListExtractsResponse,
    )
    async def list_extracts(
        response: Response,
        domain: str | None = None,
        status_filter: str | None = Query(default=None, alias="status"),
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        fund_id: str | None = None,
        limit: int = Query(default=20, le=100),
        cursor: str | None = None,
        principal: Principal = Depends(authenticate),
    ):
        storage = get_storage()
        rl = get_rate_limiter()
        for k, v in rl.check_request(principal.app_id).items():
            response.headers[k] = v

        cursor_decoded = None
        if cursor:
            try:
                cursor_decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
            except Exception:
                raise ValidationError("invalid cursor")
        statuses = status_filter.split(",") if status_filter else None
        rows = storage.list_extracts(
            principal.app_id,
            domain=domain,
            statuses=statuses,
            created_after=created_after,
            created_before=created_before,
            fund_id=fund_id,
            limit=limit,
            cursor=cursor_decoded,
        )
        next_cursor = None
        if len(rows) > limit:
            tail = rows[limit - 1]
            next_cursor = base64.urlsafe_b64encode(
                tail["created_at"].encode()
            ).decode()
            rows = rows[:limit]
        return ListExtractsResponse(
            items=[
                ListExtractsEntry(
                    extract_id=r["extract_id"],
                    domain=r["domain"],
                    status=ExtractStatus(r["status"]),
                    created_at=_parse_iso(r["created_at"]),  # type: ignore[arg-type]
                    completed_at=_parse_iso(r.get("completed_at")),
                )
                for r in rows
            ],
            next_cursor=next_cursor,
        )


def _estimate_duration(fund_scope: list[str], domain: str) -> timedelta:
    n = len(fund_scope)
    if n <= 1:
        return timedelta(minutes=5)
    if n <= 10:
        return timedelta(minutes=15)
    return timedelta(minutes=45)


_register_routes(app, "/api/v1", sandbox=False)
_register_routes(app, "/sandbox/v1", sandbox=True)


# ---------------------------------------------------------------------------
# Observability endpoints
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "timestamp": utcnow().isoformat()}


@app.get("/readyz")
async def readyz(request: Request):
    worker: Worker = request.app.state.worker
    return {
        "status": "ok",
        "queue_depth": worker.queue_depth(),
    }


@app.get("/metrics")
async def metrics():
    return get_metrics().snapshot()


@app.get("/domains")
async def domains():
    return {
        name: {
            "sources": list(d.sources),
            "schema_version": d.schema_version,
        }
        for name, d in REGISTRY.items()
    }
