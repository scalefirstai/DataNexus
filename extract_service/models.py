"""Pydantic models for REST API contract and event schemas.

Mirrors sections §3 (REST API) and §4 (Event Bus) of the spec.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ExtractStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


TERMINAL_STATUSES = {
    ExtractStatus.COMPLETED,
    ExtractStatus.FAILED,
    ExtractStatus.PARTIAL,
    ExtractStatus.CANCELLED,
    ExtractStatus.EXPIRED,
}


class Frequency(str, Enum):
    DAILY = "daily"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


class OutputFormat(str, Enum):
    PARQUET = "parquet"
    NDJSON = "ndjson"
    CSV = "csv"


class Compression(str, Enum):
    GZIP = "gzip"
    ZSTD = "zstd"
    NONE = "none"


class NotificationMode(str, Enum):
    EVENT = "event"
    WEBHOOK = "webhook"
    NONE = "none"


class Priority(str, Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class ErrorCode(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    ENTITLEMENT_DENIED = "ENTITLEMENT_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    STORAGE_WRITE_FAILED = "STORAGE_WRITE_FAILED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Request models (§3.2.1)
# ---------------------------------------------------------------------------


class Period(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def _check_order(self) -> "Period":
        if self.end < self.start:
            raise ValueError("period.end must be on or after period.start")
        return self


class OutputSpec(BaseModel):
    format: OutputFormat
    compression: Compression = Compression.GZIP
    partition_by: str = "fund_id"


class NotificationSpec(BaseModel):
    mode: NotificationMode = NotificationMode.EVENT
    topic: str | None = None
    callback_url: str | None = None

    @model_validator(mode="after")
    def _conditional_fields(self) -> "NotificationSpec":
        if self.mode == NotificationMode.EVENT and not self.topic:
            raise ValueError("notification.topic is required when mode=event")
        if self.mode == NotificationMode.WEBHOOK and not self.callback_url:
            raise ValueError("notification.callback_url is required when mode=webhook")
        if self.mode == NotificationMode.WEBHOOK and self.callback_url:
            if not self.callback_url.lower().startswith("https://"):
                raise ValueError("notification.callback_url must be an HTTPS URL")
        return self


class Requester(BaseModel):
    app_id: str
    correlation_id: str | None = None


class CreateExtractRequest(BaseModel):
    domain: str
    period: Period
    as_of: datetime | None = None
    fund_scope: list[str] | None = None
    frequency: Frequency | None = None
    output: OutputSpec
    notification: NotificationSpec | None = None
    idempotency_key: str = Field(..., max_length=128, min_length=1)
    priority: Priority = Priority.NORMAL
    requester: Requester
    retention_days: int | None = None

    @field_validator("fund_scope")
    @classmethod
    def _non_empty_scope(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(v) == 0:
            raise ValueError("fund_scope must not be empty if provided")
        return v


# ---------------------------------------------------------------------------
# Response models (§3.2)
# ---------------------------------------------------------------------------


class ExtractLinks(BaseModel):
    self: str
    cancel: str


class AcceptResponse(BaseModel):
    extract_id: str
    status: ExtractStatus
    created_at: datetime
    links: ExtractLinks
    estimated_completion: datetime | None = None
    retry_after_seconds: int = 30


class ProgressInfo(BaseModel):
    funds_completed: int
    funds_total: int
    percent: int
    current_fund: str | None = None


class ResultInfo(BaseModel):
    location: str
    file_count: int
    total_bytes: int
    total_rows: int
    checksum: str
    schema_version: str
    expires_at: datetime


class LineageInfo(BaseModel):
    sources: list[str]
    as_of: datetime | None = None
    run_id: str


class ErrorDetail(BaseModel):
    code: ErrorCode
    message: str
    retries_attempted: int = 0
    partial_result: dict[str, Any] | None = None


class StatusResponse(BaseModel):
    extract_id: str
    status: ExtractStatus
    progress: ProgressInfo | None = None
    created_at: datetime
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    duration_seconds: int | None = None
    estimated_completion: datetime | None = None
    result: ResultInfo | None = None
    lineage: LineageInfo | None = None
    error: ErrorDetail | None = None


class FileEntry(BaseModel):
    file_id: str
    partition_key: str
    format: OutputFormat
    compression: Compression
    size_bytes: int
    row_count: int
    checksum: str
    download_url: str
    url_expires_at: datetime


class ManifestLink(BaseModel):
    download_url: str
    url_expires_at: datetime


class FilesResponse(BaseModel):
    extract_id: str
    files: list[FileEntry]
    manifest: ManifestLink


class CancelResponse(BaseModel):
    extract_id: str
    status: ExtractStatus
    cancelled_at: datetime
    cancelled_by: str


class ListExtractsEntry(BaseModel):
    extract_id: str
    domain: str
    status: ExtractStatus
    created_at: datetime
    completed_at: datetime | None = None


class ListExtractsResponse(BaseModel):
    items: list[ListExtractsEntry]
    next_cursor: str | None = None


class ApiError(BaseModel):
    code: ErrorCode
    message: str
    details: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Event schemas (§4.3 / §4.4 / §4.5)
# ---------------------------------------------------------------------------


class EventScope(BaseModel):
    domain: str
    period_start: date
    period_end: date
    as_of: datetime | None = None
    fund_scope: list[str]
    frequency: Frequency | None = None


class EventArtifact(BaseModel):
    file_count: int
    total_bytes: int
    total_rows: int
    format: OutputFormat
    compression: Compression
    checksum: str
    schema_version: str
    files_endpoint: str
    expires_at: datetime


class EventLineage(BaseModel):
    run_id: str
    sources: list[str]
    produced_by: str
    duration_seconds: int


class EventRequester(BaseModel):
    app_id: str
    correlation_id: str | None = None


class BaseEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: str
    event_type: str
    event_version: Literal["1.0"] = "1.0"
    emitted_at: datetime
    source: str


class ExtractReadyEvent(BaseEvent):
    event_type: Literal["extract.ready"] = "extract.ready"
    extract_id: str
    idempotency_key: str
    scope: EventScope
    artifact: EventArtifact
    lineage: EventLineage
    requester: EventRequester


class ExtractFailedError(BaseModel):
    code: ErrorCode
    message: str
    retries_attempted: int
    partial_result: dict[str, Any] | None = None


class ExtractFailedEvent(BaseEvent):
    event_type: Literal["extract.failed"] = "extract.failed"
    extract_id: str
    idempotency_key: str
    scope: EventScope
    error: ExtractFailedError
    requester: EventRequester


class ExtractExpiringEvent(BaseEvent):
    event_type: Literal["extract.expiring"] = "extract.expiring"
    extract_id: str
    expires_at: datetime
    files_endpoint: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
