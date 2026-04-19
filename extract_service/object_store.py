"""Local-filesystem object store implementing §5 of the spec.

Implements:
- Path convention /extracts/{domain}/{yyyy}/{mm}/{extract_id}/
- Manifest.json + _SUCCESS marker
- Atomic publish protocol (staging → rename)
- HMAC-signed "presigned URL" simulation with expiry
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import get_settings
from .models import Compression, OutputFormat, utcnow


@dataclass
class WrittenFile:
    partition_key: str
    name: str
    size_bytes: int
    row_count: int
    checksum: str
    format: OutputFormat
    compression: Compression


def _compress(data: bytes, compression: Compression) -> bytes:
    if compression == Compression.GZIP:
        return gzip.compress(data)
    if compression == Compression.NONE:
        return data
    if compression == Compression.ZSTD:
        try:
            import zstandard as zstd  # type: ignore

            return zstd.ZstdCompressor().compress(data)
        except ImportError:
            return gzip.compress(data)
    return data


def _extension(fmt: OutputFormat, compression: Compression) -> str:
    base = {
        OutputFormat.PARQUET: "parquet",
        OutputFormat.NDJSON: "ndjson",
        OutputFormat.CSV: "csv",
    }[fmt]
    if compression == Compression.GZIP:
        return f"{base}.gz"
    if compression == Compression.ZSTD:
        return f"{base}.zst"
    return base


def _serialise(rows: list[dict[str, Any]], fmt: OutputFormat) -> bytes:
    if fmt == OutputFormat.NDJSON:
        return "\n".join(json.dumps(r) for r in rows).encode("utf-8") + b"\n"
    if fmt == OutputFormat.CSV:
        if not rows:
            return b""
        headers = list(rows[0].keys())
        lines = [",".join(headers)]
        for row in rows:
            lines.append(",".join(str(row.get(h, "")) for h in headers))
        return ("\n".join(lines) + "\n").encode("utf-8")
    # Parquet path: for the reference impl we emit a JSON surrogate with .parquet
    # extension. Production would use pyarrow.
    return json.dumps(
        {"_format": "parquet-surrogate", "rows": rows}, separators=(",", ":")
    ).encode("utf-8")


class ObjectStore:
    def __init__(self, root: Path):
        self.root = root
        self.staging = root / ".staging"
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)

    # ---- path helpers ----

    def published_prefix(self, domain: str, extract_id: str) -> Path:
        created_at = utcnow()
        return (
            self.root
            / "extracts"
            / domain
            / f"{created_at.year:04d}"
            / f"{created_at.month:02d}"
            / extract_id
        )

    def staging_prefix(self, extract_id: str) -> Path:
        return self.staging / extract_id

    # ---- atomic publish ----

    def write_partition(
        self,
        extract_id: str,
        partition_key: str,
        rows: list[dict[str, Any]],
        fmt: OutputFormat,
        compression: Compression,
    ) -> WrittenFile:
        prefix = self.staging_prefix(extract_id)
        prefix.mkdir(parents=True, exist_ok=True)
        raw = _serialise(rows, fmt)
        compressed = _compress(raw, compression)
        filename = f"{partition_key}.{_extension(fmt, compression)}"
        path = prefix / filename
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(compressed)
        os.replace(tmp, path)
        checksum = "sha256:" + hashlib.sha256(compressed).hexdigest()
        return WrittenFile(
            partition_key=partition_key,
            name=filename,
            size_bytes=len(compressed),
            row_count=len(rows),
            checksum=checksum,
            format=fmt,
            compression=compression,
        )

    def write_manifest(
        self,
        extract_id: str,
        manifest: dict[str, Any],
    ) -> str:
        prefix = self.staging_prefix(extract_id)
        prefix.mkdir(parents=True, exist_ok=True)
        path = prefix / "manifest.json"
        data = json.dumps(manifest, indent=2, default=str).encode("utf-8")
        path.write_bytes(data)
        return "sha256:" + hashlib.sha256(data).hexdigest()

    def promote(self, extract_id: str, domain: str) -> Path:
        """Atomic promote: rename staging/ prefix to published path + _SUCCESS."""
        staging = self.staging_prefix(extract_id)
        if not staging.exists():
            raise FileNotFoundError(
                f"staging prefix missing for extract {extract_id}"
            )
        destination = self.published_prefix(domain, extract_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        os.rename(staging, destination)
        (destination / "_SUCCESS").write_bytes(b"")
        return destination

    def cleanup_staging(self, extract_id: str) -> None:
        staging = self.staging_prefix(extract_id)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    def delete_published(self, extract_id: str, domain: str) -> None:
        candidate = self._find_published(extract_id, domain)
        if candidate and candidate.exists():
            shutil.rmtree(candidate, ignore_errors=True)

    def _find_published(self, extract_id: str, domain: str) -> Path | None:
        base = self.root / "extracts" / domain
        if not base.exists():
            return None
        for year_dir in base.iterdir():
            if not year_dir.is_dir():
                continue
            for month_dir in year_dir.iterdir():
                candidate = month_dir / extract_id
                if candidate.exists():
                    return candidate
        return None

    def file_path(
        self, extract_id: str, domain: str, filename: str
    ) -> Path | None:
        prefix = self._find_published(extract_id, domain)
        if not prefix:
            return None
        path = prefix / filename
        return path if path.exists() else None

    # ---- presigned URLs ----

    def sign(
        self,
        extract_id: str,
        domain: str,
        filename: str,
        app_id: str,
        ttl_seconds: int | None = None,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        settings = get_settings()
        ttl = ttl_seconds or settings.presigned_url_ttl_seconds
        expires = (now or utcnow()) + timedelta(seconds=ttl)
        expires_ts = int(expires.timestamp())
        payload = f"{extract_id}|{domain}|{filename}|{app_id}|{expires_ts}"
        token = hmac.new(
            settings.presign_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{token}.{expires_ts}.{app_id}", expires

    def verify(
        self,
        extract_id: str,
        domain: str,
        filename: str,
        token: str,
    ) -> tuple[bool, str | None, str | None]:
        try:
            digest, expires_ts_str, app_id = token.split(".", 2)
            expires_ts = int(expires_ts_str)
        except (ValueError, AttributeError):
            return False, None, "malformed token"
        if expires_ts < int(utcnow().timestamp()):
            return False, None, "token expired"
        payload = f"{extract_id}|{domain}|{filename}|{app_id}|{expires_ts}"
        expected = hmac.new(
            get_settings().presign_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, digest):
            return False, None, "bad signature"
        return True, app_id, None


_store: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    global _store
    if _store is None:
        _store = ObjectStore(get_settings().object_store_root)
    return _store


def reset_object_store_for_tests() -> None:
    global _store
    _store = None
