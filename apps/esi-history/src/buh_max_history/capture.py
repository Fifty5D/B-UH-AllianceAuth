"""Fail-open, change-only archival hooks for django-esi responses."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import OperationalError, ProgrammingError, transaction
from django.db.models import F
from django.utils.timezone import now

logger = logging.getLogger(__name__)

_HOOK_LOCK = threading.Lock()
_HOOKS_INSTALLED = False
_CAPTURE_LOCAL = threading.local()
_CONFIG_LOCK = threading.Lock()
_CONFIG_CACHE: tuple[float, dict[str, Any]] | None = None
_DISK_CACHE: tuple[float, int] | None = None
_LOGGED_AT: dict[str, float] = {}
_SENSITIVE_KEY = re.compile(
    r"(?:token|secret|authorization|password|credential|api[_-]?key|refresh|body)",
    re.IGNORECASE,
)
_SAFE_HEADERS = {
    "cache-control",
    "content-length",
    "content-type",
    "date",
    "etag",
    "expires",
    "last-modified",
    "x-esi-error-limit-remain",
    "x-esi-error-limit-reset",
    "x-pages",
}


def archive_root() -> Path:
    return Path(getattr(settings, "BUH_ESI_ARCHIVE_ROOT", "/var/lib/buh-esi-archive"))


def _warn_once(key: str, message: str, *args) -> None:
    current = time.monotonic()
    if current - _LOGGED_AT.get(key, 0) >= 300:
        _LOGGED_AT[key] = current
        logger.warning(message, *args)


def _configuration() -> dict[str, Any]:
    global _CONFIG_CACHE
    current = time.monotonic()
    with _CONFIG_LOCK:
        if _CONFIG_CACHE and current - _CONFIG_CACHE[0] < 60:
            return _CONFIG_CACHE[1]
        defaults = {
            "capture_enabled": getattr(settings, "BUH_ESI_ARCHIVE_CAPTURE_ENABLED", True),
            "capture_public_esi": getattr(settings, "BUH_ESI_ARCHIVE_CAPTURE_PUBLIC", True),
            "capture_private_esi": getattr(
                settings, "BUH_ESI_ARCHIVE_CAPTURE_PRIVATE", True
            ),
            "max_response_mib": getattr(settings, "BUH_ESI_ARCHIVE_MAX_RESPONSE_MIB", 512),
            "minimum_free_gib": getattr(settings, "BUH_ESI_ARCHIVE_MINIMUM_FREE_GIB", 25),
        }
        try:
            from .models import ArchiveConfiguration

            config = ArchiveConfiguration.objects.filter(singleton_id=1).first()
            if config:
                defaults.update(
                    capture_enabled=config.capture_enabled,
                    capture_public_esi=config.capture_public_esi,
                    capture_private_esi=config.capture_private_esi,
                    max_response_mib=config.max_response_mib,
                    minimum_free_gib=config.minimum_free_gib,
                )
        except (OperationalError, ProgrammingError):
            # Expected while the package is installed before its first migration.
            pass
        _CONFIG_CACHE = (current, defaults)
        return defaults


def reset_configuration_cache() -> None:
    global _CONFIG_CACHE, _DISK_CACHE
    with _CONFIG_LOCK:
        _CONFIG_CACHE = None
        _DISK_CACHE = None


def _free_bytes() -> int:
    global _DISK_CACHE
    current = time.monotonic()
    if _DISK_CACHE and current - _DISK_CACHE[0] < 60:
        return _DISK_CACHE[1]
    root = archive_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    free = shutil.disk_usage(root).free
    _DISK_CACHE = (current, free)
    return free


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item, depth + 1)
            for key, item in value.items()
            if not _SENSITIVE_KEY.search(str(key))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item, depth + 1) for item in value]
    if is_dataclass(value):
        return _json_safe(asdict(value), depth + 1)
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump(mode="json"), depth + 1)
        except Exception as exc:  # noqa: BLE001 - fallback is deliberately defensive
            logger.debug("Could not serialize model_dump value: %s", exc)
    if hasattr(value, "dict"):
        try:
            return _json_safe(value.dict(), depth + 1)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not serialize dict-like value: %s", exc)
    return str(value)[:1000]


def _safe_parameters(operation, extra=None) -> dict[str, Any]:
    raw = (getattr(operation, "_kwargs", {}) or {}) | (extra or {})
    return {
        str(key): _json_safe(value)
        for key, value in raw.items()
        if not _SENSITIVE_KEY.search(str(key))
    }


def _payload_bytes(data: Any, response: Any) -> bytes:
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    return json.dumps(
        _json_safe(data),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_response_headers(response: Any) -> dict[str, str]:
    headers = getattr(response, "headers", {}) or {}
    return {
        str(key).lower(): str(value)[:1000]
        for key, value in headers.items()
        if str(key).lower() in _SAFE_HEADERS
    }


def _operation_metadata(operation, extra=None) -> dict[str, Any]:
    token = getattr(operation, "token", None)
    is_private = bool(token)
    character_id = None
    auth_user_id = None
    if token and not isinstance(token, str):
        character_id = getattr(token, "character_id", None)
        auth_user_id = getattr(token, "user_id", None)
    operation_spec = getattr(operation, "operation", None)
    operation_id = str(getattr(operation_spec, "operationId", "") or "unknown_operation")[
        :180
    ]
    method = str(getattr(operation, "method", "GET") or "GET").upper()[:12]
    url = str(getattr(operation, "url", "") or "")[:500]
    app_name = str(getattr(getattr(operation, "api", None), "app_name", "") or "")[:160]
    parameters = _safe_parameters(operation, extra)
    identity = {
        "operation_id": operation_id,
        "method": method,
        "url": url,
        "parameters": parameters,
        "private": is_private,
        "character_id": character_id,
    }
    stream_key = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "stream_key": stream_key,
        "operation_id": operation_id,
        "method": method,
        "url_template": url,
        "safe_parameters": parameters,
        "app_name": app_name,
        "is_private": is_private,
        "character_id": character_id,
        "auth_user_id": auth_user_id,
    }


def _record_issue(operation_id: str, reason: str, detail: str = "") -> None:
    try:
        from .models import ArchiveCaptureIssue

        issue, created = ArchiveCaptureIssue.objects.get_or_create(
            operation_id=operation_id[:180],
            reason=reason[:80],
            detail=detail[:500],
        )
        if not created:
            ArchiveCaptureIssue.objects.filter(pk=issue.pk).update(
                occurrences=F("occurrences") + 1,
                last_seen_at=now(),
            )
    except (OperationalError, ProgrammingError):
        pass


def _stream(metadata: dict[str, Any]):
    from .models import ArchiveStream

    defaults = {key: value for key, value in metadata.items() if key != "stream_key"}
    stream, _ = ArchiveStream.objects.get_or_create(
        stream_key=metadata["stream_key"], defaults=defaults
    )
    return stream


def _relative_payload_path(metadata: dict[str, Any], payload_hash: str) -> Path:
    visibility = "private" if metadata["is_private"] else "public-esi"
    operation = (
        re.sub(r"[^A-Za-z0-9_.-]+", "-", metadata["operation_id"]).strip("-")[:120]
        or "unknown"
    )
    stamp = now()
    return Path(
        visibility,
        operation,
        f"{stamp:%Y}",
        f"{stamp:%m}",
        metadata["stream_key"][:12],
        f"{payload_hash}.json.gz",
    )


def _write_payload(relative: Path, payload: bytes) -> int:
    target = archive_root() / relative
    if target.exists():
        return target.stat().st_size
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    compressed = gzip.compress(payload, compresslevel=6, mtime=0)
    if (
        shutil.disk_usage(target.parent).free - len(compressed)
        < int(_configuration()["minimum_free_gib"]) * 1024**3
    ):
        raise OSError("Archive free-space reserve reached.")
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(compressed)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.replace(temporary, target)
        except FileExistsError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target.stat().st_size


def archive_esi_result(operation, data: Any, response: Any, extra=None):
    """Archive one successful ESI response without ever affecting its caller."""

    config = _configuration()
    if not config["capture_enabled"]:
        return
    metadata = _operation_metadata(operation, extra)
    if metadata["method"] not in {"GET", "HEAD"}:
        return
    if metadata["is_private"] and not config["capture_private_esi"]:
        return
    if not metadata["is_private"] and not config["capture_public_esi"]:
        return
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code and not 200 <= status_code < 300:
        return
    payload = _payload_bytes(data, response)
    if not payload:
        return

    maximum = int(config["max_response_mib"]) * 1024 * 1024
    minimum_free = int(config["minimum_free_gib"]) * 1024 * 1024 * 1024
    skipped_reason = ""
    if len(payload) > maximum:
        skipped_reason = "response_too_large"
    elif _free_bytes() - len(payload) < minimum_free:
        skipped_reason = "minimum_free_space_guard"

    from .models import ArchiveSnapshot, ArchiveStream

    with transaction.atomic():
        stream = _stream(metadata)
        if skipped_reason:
            ArchiveStream.objects.filter(pk=stream.pk).update(
                request_count=F("request_count") + 1,
                source_bytes=F("source_bytes") + len(payload),
                skipped_count=F("skipped_count") + 1,
                last_status_code=status_code,
                last_seen_at=now(),
            )
            _record_issue(metadata["operation_id"], skipped_reason)
            return

    payload_hash = hashlib.sha256(payload).hexdigest()
    with transaction.atomic():
        # Serialize only this stream, preserving arrival order without blocking
        # unrelated characters/endpoints. Old snapshots retain their identity.
        stream = ArchiveStream.objects.select_for_update().get(pk=stream.pk)
        existing = ArchiveSnapshot.objects.filter(
            stream=stream, payload_sha256=payload_hash
        ).first()
        relative = (
            Path(existing.relative_path)
            if existing
            else _relative_payload_path(metadata, payload_hash)
        )
        stored_bytes = _write_payload(relative, payload)
        snapshot, created = ArchiveSnapshot.objects.get_or_create(
            stream=stream,
            payload_sha256=payload_hash,
            defaults={
                "relative_path": relative.as_posix(),
                "content_type": str(
                    getattr(response, "headers", {}).get("content-type", "")
                )[:120],
                "status_code": status_code or 200,
                "source_bytes": len(payload),
                "stored_bytes": stored_bytes,
                "response_headers": _safe_response_headers(response),
            },
        )
        stamp = now()
        if not created:
            ArchiveSnapshot.objects.filter(pk=snapshot.pk).update(
                observation_count=F("observation_count") + 1,
                last_observed_at=stamp,
            )
        from .models import ArchiveObservation

        current = stream.observations.first()
        if current and current.snapshot_id == snapshot.pk:
            ArchiveObservation.objects.filter(pk=current.pk).update(
                last_observed_at=stamp, observation_count=F("observation_count") + 1
            )
        else:
            ArchiveObservation.objects.create(
                stream=stream,
                snapshot=snapshot,
                first_observed_at=stamp,
                last_observed_at=stamp,
            )
        ArchiveStream.objects.filter(pk=stream.pk).update(
            request_count=F("request_count") + 1,
            snapshot_count=F("snapshot_count") + int(created),
            source_bytes=F("source_bytes") + len(payload),
            stored_bytes=F("stored_bytes") + (stored_bytes if created else 0),
            current_payload_sha256=payload_hash,
            last_status_code=status_code or 200,
            last_seen_at=stamp,
        )
        return snapshot


def _safe_archive(operation, data: Any, response: Any, extra=None) -> None:
    if getattr(_CAPTURE_LOCAL, "active", False):
        return
    _CAPTURE_LOCAL.active = True
    try:
        archive_esi_result(operation, data, response, extra)
    except Exception as exc:  # noqa: BLE001 - archival must never break Alliance Auth
        operation_id = str(
            getattr(getattr(operation, "operation", None), "operationId", "unknown")
        )
        _warn_once(
            f"capture:{type(exc).__name__}",
            "ESI archive capture failed open for %s: %s",
            operation_id,
            exc,
        )
        try:
            _record_issue(operation_id, "capture_error", f"{type(exc).__name__}: {exc}")
        except Exception as issue_exc:  # noqa: BLE001
            logger.debug("Could not persist archive capture issue: %s", issue_exc)
    finally:
        _CAPTURE_LOCAL.active = False


def install_esi_archive_hooks() -> None:
    """Patch django-esi's result boundary once per process."""

    global _HOOKS_INSTALLED
    with _HOOK_LOCK:
        if _HOOKS_INSTALLED:
            return
        try:
            from esi.openapi_clients import EsiOperation, EsiOperationAsync
        except ImportError:
            return

        original_sync = EsiOperation.result
        if not getattr(original_sync, "_buh_archive_wrapper", False):

            def result(
                self,
                use_etag=True,
                return_response=False,
                force_refresh=False,
                use_cache=True,
                store_cache=True,
                last_modified=None,
                **extra,
            ):
                data, response = original_sync(
                    self,
                    use_etag=use_etag,
                    return_response=True,
                    force_refresh=force_refresh,
                    use_cache=use_cache,
                    store_cache=store_cache,
                    last_modified=last_modified,
                    **extra,
                )
                _safe_archive(self, data, response, extra)
                return (data, response) if return_response else data

            result._buh_archive_wrapper = True
            result._buh_archive_original = original_sync
            EsiOperation.result = result

        original_async = EsiOperationAsync.result
        if not getattr(original_async, "_buh_archive_wrapper", False):

            async def result_async(
                self,
                etag=None,
                return_response=False,
                use_cache=True,
                last_modified=None,
                **extra,
            ):
                data, response = await original_async(
                    self,
                    etag=etag,
                    return_response=True,
                    use_cache=use_cache,
                    last_modified=last_modified,
                    **extra,
                )
                await asyncio.to_thread(_safe_archive, self, data, response, extra)
                return (data, response) if return_response else data

            result_async._buh_archive_wrapper = True
            result_async._buh_archive_original = original_async
            EsiOperationAsync.result = result_async

        _HOOKS_INSTALLED = True
