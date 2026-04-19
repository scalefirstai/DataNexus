"""Runtime configuration for the extract service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    base_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("EXTRACT_BASE_DIR", "./data")).resolve()
    )
    db_path: Path = field(init=False)
    object_store_root: Path = field(init=False)
    audit_log_path: Path = field(init=False)

    presigned_url_ttl_seconds: int = 3600
    default_retention_days: int = 7
    regulatory_retention_days: int = 90
    idempotency_key_ttl_days: int = 30
    event_retention_days: int = 7

    worker_concurrency: int = 4
    worker_retry_base_seconds: float = 5.0
    worker_retry_max_seconds: float = 300.0
    worker_retry_jitter: float = 0.2
    worker_max_attempts_per_fund: int = 3
    worker_max_attempts_per_extract: int = 5

    webhook_timeout_seconds: float = 10.0
    webhook_backoff_schedule: tuple = (10, 30, 90, 270, 810)

    rate_limit_requests_per_minute: int = 60
    rate_limit_concurrent_extracts: int = 10
    rate_limit_presign_per_minute: int = 120
    max_period_years: int = 5
    max_fund_scope: int = 500

    presign_secret: str = field(
        default_factory=lambda: os.environ.get(
            "EXTRACT_PRESIGN_SECRET", "dev-presign-secret-change-me"
        )
    )
    webhook_signing_secret_default: str = "dev-webhook-secret"

    sandbox_mode: bool = False

    def __post_init__(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.base_dir / "extract.db"
        self.object_store_root = self.base_dir / "object-store"
        self.audit_log_path = self.base_dir / "audit.log"
        self.object_store_root.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests(base_dir: Path) -> Settings:
    """Rebuild settings anchored to a different base dir (used in tests)."""
    global _settings
    os.environ["EXTRACT_BASE_DIR"] = str(base_dir)
    _settings = Settings()
    return _settings
