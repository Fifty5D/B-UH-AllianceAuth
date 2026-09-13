"""Bounded, resumable mirroring of explicitly configured EVE Ref datasets."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shutil
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.core.exceptions import ValidationError
from django.db.models import Max, Q
from django.utils.timezone import now

from .capture import archive_root
from .job_lock import ArchiveLockLost, assert_public_archive_lock
from .models import (
    ArchiveConfiguration,
    PublicArchiveFile,
    PublicCatalogIndex,
    PublicDataset,
)

USER_AGENT = "B-UH-ESI-History-Archive/2.0.0 (+https://auth.b-uh.com/)"
MAX_CATALOG_ENTRIES_PER_RUN = 2000
MAX_CATALOG_INDEXES_PER_RUN = 250


def validate_everef_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "data.everef.net":
        raise ValidationError("Public archive URLs must use https://data.everef.net/.")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValidationError("Credentials and non-standard ports are not allowed.")
    decoded = unquote(parsed.path)
    if "\x00" in decoded or any(part == ".." for part in PurePosixPath(decoded).parts):
        raise ValidationError("The public archive URL contains an unsafe path.")
    return url


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_everef_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen(request, *, timeout):
    return build_opener(_SafeRedirect()).open(request, timeout=timeout)


def _etag(value):
    # HTTP quotes are syntax. Preserve W/ so genuinely different validators
    # still invalidate an existing download.
    text = str(value).strip()
    return (
        ("W/" + text[2:].strip().strip('"')) if text.startswith("W/") else text.strip('"')
    )


def _modified(value):
    try:
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            stamp = parsedate_to_datetime(value)
        return stamp.astimezone(UTC).replace(microsecond=0).isoformat()
    except (ValueError, TypeError, AttributeError):
        return str(value)


def _rate_limit_delay(exc):
    if not isinstance(exc, HTTPError) or exc.code not in (420, 429, 503):
        return 0
    value = (exc.headers or {}).get("Retry-After", "")
    try:
        return max(60, int(value))
    except (ValueError, TypeError):
        try:
            return max(60, int((parsedate_to_datetime(value) - now()).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 900


def _rate_limited():
    return ArchiveConfiguration.objects.filter(
        singleton_id=1, public_retry_at__gt=now()
    ).exists()


def _pause_for_rate_limit(exc):
    delay = _rate_limit_delay(exc)
    if delay:
        ArchiveConfiguration.objects.filter(singleton_id=1).update(
            public_retry_at=now() + timedelta(seconds=delay)
        )
    return delay


def _request_json(url: str, timeout: int = 30) -> tuple[Any, dict[str, str]]:
    validate_everef_url(url)
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        chunks = []
        size = 0
        deadline = time.monotonic() + 60
        reader = getattr(response, "read1", response.read)
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("EVE Ref index exceeded the request time limit.")
            chunk = reader(min(1024 * 1024, 64 * 1024 * 1024 - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > 64 * 1024 * 1024:
                raise RuntimeError("EVE Ref index exceeded the 64 MiB safety limit.")
            chunks.append(chunk)
        payload = json.loads(b"".join(chunks))
        headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
    return payload, headers


def _index_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    entries: list[dict[str, Any]] = []
    files = payload.get("files")
    if isinstance(files, list):
        entries.extend(item for item in files if isinstance(item, dict))
    elif isinstance(files, dict):
        entries.extend(
            ({"name": name, **item} if isinstance(item, dict) else {"name": name})
            for name, item in files.items()
        )
    directories = payload.get("directories")
    if isinstance(directories, list):
        entries.extend(
            {**item, "_buh_directory": True}
            for item in directories
            if isinstance(item, dict)
        )
    elif isinstance(directories, dict):
        entries.extend(
            {
                "name": name,
                **(item if isinstance(item, dict) else {}),
                "_buh_directory": True,
            }
            for name, item in directories.items()
        )
    if entries:
        return entries
    for key in ("entries", "items", "children", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [
                ({"name": name, **item} if isinstance(item, dict) else {"name": name})
                for name, item in value.items()
            ]
    # Some directory indexes are a direct name -> metadata mapping.
    if payload and all(isinstance(value, dict) for value in payload.values()):
        return [{"name": name, **value} for name, value in payload.items()]
    return []


def _entry_name(entry: dict[str, Any]) -> str:
    return str(
        entry.get("name")
        or entry.get("path")
        or entry.get("key")
        or entry.get("filename")
        or entry.get("url")
        or ""
    ).strip()


def _entry_url(index_url: str, entry: dict[str, Any]) -> str:
    explicit = str(
        entry.get("index_url") or entry.get("url") or entry.get("href") or ""
    ).strip()
    candidate = explicit or _entry_name(entry)
    return urljoin(index_url, candidate)


def _is_directory(entry: dict[str, Any], url: str) -> bool:
    kind = str(entry.get("type") or entry.get("kind") or "").lower()
    return bool(
        entry.get("_buh_directory")
        or entry.get("index_url")
        or kind in {"directory", "dir", "folder"}
        or urlparse(url).path.endswith("/")
    )


def _remote_size(entry: dict[str, Any]) -> int:
    for key in ("size", "bytes", "content_length", "content-length"):
        if entry.get(key) is None:
            continue
        try:
            return max(0, int(entry.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return 0


def _safe_relative_path(dataset: PublicDataset, source_url: str) -> str:
    validate_everef_url(source_url)
    parsed = urlparse(source_url)
    normalized = posixpath.normpath(unquote(parsed.path)).lstrip("/")
    if not normalized or normalized == "." or normalized.startswith("../"):
        raise ValidationError("The public archive file path is unsafe.")
    return (Path("public", dataset.slug) / Path(normalized)).as_posix()


def _catalog_file(
    dataset: PublicDataset, source_url: str, entry: dict[str, Any]
) -> tuple[int, int]:
    assert_public_archive_lock()
    relative = _safe_relative_path(dataset, source_url)
    size = _remote_size(entry)
    etag = str(entry.get("etag") or entry.get("ETag") or "")[:300]
    modified = str(
        entry.get("last_modified") or entry.get("modified") or entry.get("mtime") or ""
    )[:120]
    from django.utils.dateparse import parse_datetime

    try:
        source_time = parse_datetime(str(entry.get("file_time") or ""))
        if source_time is not None and source_time.tzinfo is None:
            source_time = None
    except ValueError:
        source_time = None
    record, was_created = PublicArchiveFile.objects.get_or_create(
        source_url=source_url,
        defaults={
            "dataset": dataset,
            "relative_path": relative,
            "remote_size": size,
            "etag": etag,
            "remote_modified": modified,
            "source_time": source_time,
        },
    )
    if was_created:
        return 1, 0
    altered = bool(
        (size and size != record.remote_size)
        or (etag and record.etag and _etag(etag) != _etag(record.etag))
        or (
            modified
            and record.remote_modified
            and _modified(modified) != _modified(record.remote_modified)
        )
    )
    updates = {
        "dataset": dataset,
        "relative_path": relative,
        "remote_size": size or record.remote_size,
        "etag": etag or record.etag,
        "remote_modified": modified or record.remote_modified,
        "source_time": source_time or record.source_time,
        "last_checked_at": now(),
    }
    if altered:
        updates.update(
            status=PublicArchiveFile.Status.PENDING,
            last_error="",
            retry_at=None,
            failure_count=0,
        )
    PublicArchiveFile.objects.filter(pk=record.pk).update(**updates)
    return 0, int(altered)


def _candidate_indexes(dataset, limit):
    # Keep discovering new files at the root, then share the remaining slots
    # between unfinished work, failed indexes and previously cataloged history.
    root = dataset.indexes.get(source_url=dataset.index_url)
    candidates = [root]
    remaining = max(0, limit - 1)
    for status, allowance in (
        (PublicCatalogIndex.Status.PENDING, max(1, remaining // 2)),
        (PublicCatalogIndex.Status.FAILED, max(1, remaining // 4)),
        (PublicCatalogIndex.Status.CATALOGED, remaining),
        (PublicCatalogIndex.Status.PENDING, remaining),
        (PublicCatalogIndex.Status.FAILED, remaining),
    ):
        available = limit - len(candidates)
        if available <= 0:
            break
        candidates.extend(
            dataset.indexes.filter(status=status)
            .exclude(pk__in=[item.pk for item in candidates])
            .order_by("last_checked_at", "pk")[: min(available, allowance)]
        )
    return candidates


def catalog_dataset(dataset, *, max_indexes=MAX_CATALOG_INDEXES_PER_RUN, deadline=None):
    """Persist a cursor so a large index resumes across bounded batches."""
    deadline = deadline if deadline is not None else time.monotonic() + 300
    validate_everef_url(dataset.index_url)
    PublicCatalogIndex.objects.get_or_create(
        dataset=dataset, source_url=dataset.index_url, defaults={"depth": 0}
    )
    indexes = _candidate_indexes(dataset, max(1, max_indexes))
    discovered = created = changed = processed = 0
    errors = []
    for index in indexes:
        if time.monotonic() >= deadline or discovered >= MAX_CATALOG_ENTRIES_PER_RUN:
            break
        if _rate_limited():
            break
        position = index.cursor
        digest = index.catalog_sha256
        try:
            payload, _headers = _request_json(index.source_url)
            entries = _index_entries(payload)
            digest = hashlib.sha256(
                json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if digest != index.catalog_sha256:
                position = 0
            while position < len(entries):
                if (
                    time.monotonic() >= deadline
                    or discovered >= MAX_CATALOG_ENTRIES_PER_RUN
                ):
                    break
                entry = entries[position]
                source_url = _entry_url(index.source_url, entry)
                validate_everef_url(source_url)
                if _is_directory(entry, source_url):
                    if not urlparse(source_url).path.endswith("/index.json"):
                        source_url = urljoin(source_url.rstrip("/") + "/", "index.json")
                    validate_everef_url(source_url)
                    PublicCatalogIndex.objects.get_or_create(
                        dataset=dataset,
                        source_url=source_url,
                        defaults={"depth": min(index.depth + 1, 65535)},
                    )
                elif not source_url.endswith("/index.json"):
                    added, altered = _catalog_file(dataset, source_url, entry)
                    created += added
                    changed += altered
                position += 1
                discovered += 1
                if position % 100 == 0:
                    PublicCatalogIndex.objects.filter(pk=index.pk).update(
                        cursor=position,
                        catalog_sha256=digest,
                        status=PublicCatalogIndex.Status.PENDING,
                        last_checked_at=now(),
                    )
            complete = position >= len(entries)
            updates = {
                "cursor": 0 if complete else position,
                "catalog_sha256": digest,
                "last_checked_at": now(),
                "last_error": "",
                "status": PublicCatalogIndex.Status.CATALOGED
                if complete
                else PublicCatalogIndex.Status.PENDING,
            }
            if complete:
                updates["last_catalog_at"] = now()
                processed += 1
            PublicCatalogIndex.objects.filter(pk=index.pk).update(**updates)
        except ArchiveLockLost:
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:1000]
            PublicCatalogIndex.objects.filter(pk=index.pk).update(
                status=PublicCatalogIndex.Status.FAILED,
                last_error=message,
                last_checked_at=now(),
                cursor=position,
                catalog_sha256=digest,
            )
            errors.append({"url": index.source_url, "error": message})
            if _pause_for_rate_limit(exc):
                break
    updates = {"last_error": errors[0]["error"] if errors else ""}
    if processed and not errors:
        updates["last_catalog_at"] = now()
    PublicDataset.objects.filter(pk=dataset.pk).update(**updates)
    return {
        "dataset": dataset.slug,
        "discovered": discovered,
        "created": created,
        "changed": changed,
        "indexes": processed,
        "pending_indexes": dataset.indexes.exclude(
            status=PublicCatalogIndex.Status.CATALOGED
        ).count(),
        "errors": errors[:25],
    }


def catalog_enabled_datasets():
    results, errors = [], []
    deadline = time.monotonic() + 300
    datasets = list(
        PublicDataset.objects.filter(enabled=True).order_by(
            "last_catalog_at", "priority", "pk"
        )
    )
    per_dataset = max(1, MAX_CATALOG_INDEXES_PER_RUN // max(1, len(datasets)))
    for dataset in datasets:
        if time.monotonic() >= deadline or _rate_limited():
            break
        try:
            result = catalog_dataset(
                dataset,
                max_indexes=per_dataset,
                deadline=min(deadline, time.monotonic() + 300 / max(1, len(datasets))),
            )
            results.append(result)
            errors.extend({"dataset": dataset.slug, **item} for item in result["errors"])
        except ArchiveLockLost:
            raise
        except Exception as exc:
            errors.append({"dataset": dataset.slug, "error": str(exc)[:1000]})
    return {
        "datasets": results,
        "blocked": "remote_rate_limit" if _rate_limited() else "",
        "errors": errors[:25],
        "discovered": sum(item["discovered"] for item in results),
        "created": sum(item["created"] for item in results),
        "changed": sum(item["changed"] for item in results),
    }


class StoragePaused(RuntimeError):
    pass


class DownloadBudgetExceeded(RuntimeError):
    pass


def _download_file(record, minimum_free_bytes, *, budget_bytes, deadline):
    """Stream to a temporary sibling; only complete validated files replace data."""
    validate_everef_url(record.source_url)
    root = archive_root().resolve()
    target = (root / record.relative_path).resolve()
    if not target.is_relative_to(root):
        raise ValidationError("Archive file path escapes the shared archive directory.")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(root).free - max(record.remote_size, 1) < minimum_free_bytes:
        raise StoragePaused("Minimum free-space guard reached; public mirroring paused.")
    PublicArchiveFile.objects.filter(pk=record.pk).update(
        status=PublicArchiveFile.Status.DOWNLOADING, last_error=""
    )
    # Deterministic, operation-owned temporary file: the next lock holder can
    # replace an interrupted partial without ever removing an archived payload.
    temporary = target.with_name("." + target.name + ".download-part")
    descriptor = os.open(
        temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    digest = hashlib.sha256()
    written = received = 0
    request = Request(
        record.source_url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
    )
    try:
        with (
            os.fdopen(descriptor, "wb") as handle,
            urlopen(request, timeout=30) as response,
        ):
            if getattr(response, "status", 200) != 200:
                raise RuntimeError("Public archive server did not return a complete file.")
            headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            length = int(headers.get("content-length", 0) or 0)
            if max(record.remote_size, length) > budget_bytes:
                raise DownloadBudgetExceeded(
                    "File exceeds the remaining per-run byte budget."
                )
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Public download reached the run time limit.")
                reader = getattr(response, "read1", response.read)
                chunk = reader(min(1024 * 1024, budget_bytes - written + 1))
                received += len(chunk)
                if not chunk:
                    break
                if written + len(chunk) > budget_bytes:
                    raise DownloadBudgetExceeded(
                        "File exceeded the remaining per-run byte budget."
                    )
                if shutil.disk_usage(root).free - len(chunk) < minimum_free_bytes:
                    raise StoragePaused(
                        "Minimum free-space guard reached; public mirroring paused."
                    )
                handle.write(chunk)
                digest.update(chunk)
                written += len(chunk)
            if (record.remote_size and written != record.remote_size) or (
                length and written != length
            ):
                raise RuntimeError(
                    "Downloaded size differs from the catalog or response length."
                )
            if (
                record.etag
                and headers.get("etag")
                and _etag(record.etag) != _etag(headers["etag"])
            ):
                raise RuntimeError(
                    "File changed since cataloging; waiting for the next catalog refresh."
                )
            handle.flush()
            os.fsync(handle.fileno())
        assert_public_archive_lock()
        from .revisions import retain_public_revision

        if target.is_file() and not record.revisions.exists():
            retain_public_revision(
                record, target, deadline=deadline, observed_at=record.downloaded_at
            )
        retain_public_revision(
            record, temporary, deadline=deadline, digest=digest.hexdigest()
        )
        os.replace(temporary, target)
        PublicArchiveFile.objects.filter(pk=record.pk).update(
            status=PublicArchiveFile.Status.STORED,
            stored_bytes=written,
            payload_sha256=digest.hexdigest(),
            downloaded_at=now(),
            last_checked_at=now(),
            failure_count=0,
            retry_at=None,
            last_error="",
            # Catalog validators remain in catalog format; do not overwrite them
            # with quoted HTTP ETags or RFC-formatted Last-Modified values.
        )
        return written
    except Exception as exc:
        # Charge partial transfers to this run too, even when the file is rejected.
        exc.archive_bytes = received
        raise
    finally:
        temporary.unlink(missing_ok=True)


def sync_public_archive(*, catalog_first=True):
    config = ArchiveConfiguration.get_solo()
    if not config.public_mirror_enabled:
        return {"enabled": False, "downloaded_files": 0, "errors": []}
    if _rate_limited():
        return {"enabled": True, "blocked": "remote_rate_limit", "errors": []}
    root = archive_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    minimum_free = int(config.minimum_free_gib) * 1024**3
    if shutil.disk_usage(root).free <= minimum_free:
        return {"enabled": True, "blocked": "minimum_free_space", "errors": []}
    deadline = time.monotonic() + 1200
    catalog = catalog_enabled_datasets() if catalog_first else {}
    max_attempts = int(config.public_max_files_per_run)
    max_bytes = int(config.public_max_gib_per_run) * 1024**3
    attempted = downloaded = transferred = stored = 0
    errors = list(catalog.get("errors", []))[:25]
    blocked = ""
    # Rotate datasets by their oldest actual download attempt. Each gets one
    # turn per round; a large high-priority backlog cannot starve other datasets.
    datasets = list(
        PublicDataset.objects.filter(enabled=True)
        .annotate(last_attempt=Max("files__last_attempt_at"))
        .order_by("last_attempt", "priority", "pk")
    )
    while (
        attempted < max_attempts and transferred < max_bytes and time.monotonic() < deadline
    ):
        progressed = False
        for dataset in datasets:
            if (
                attempted >= max_attempts
                or transferred >= max_bytes
                or time.monotonic() >= deadline
            ):
                break
            if _rate_limited():
                blocked = "remote_rate_limit"
                break
            assert_public_archive_lock()
            candidates = dataset.files.filter(
                status__in=(
                    PublicArchiveFile.Status.PENDING,
                    PublicArchiveFile.Status.FAILED,
                ),
                remote_size__lte=max_bytes - transferred,
            ).filter(Q(retry_at__isnull=True) | Q(retry_at__lte=now()))
            retry = candidates.filter(status=PublicArchiveFile.Status.FAILED).order_by(
                "retry_at", "last_attempt_at", "pk"
            )
            pending = candidates.filter(status=PublicArchiveFile.Status.PENDING)
            # Reserve turns for retries, new snapshots, and historical catch-up.
            # No category can be starved by a continuously growing catalog.
            order = (
                ("-discovered_at", "-pk")
                if dataset.download_turn == 0
                else ("discovered_at", "pk")
            )
            fresh = pending.order_by(*order)
            first, second = (retry, fresh) if dataset.download_turn == 2 else (fresh, retry)
            record = first.first() or second.first()
            if record is None:
                continue
            progressed = True
            dataset.download_turn = (dataset.download_turn + 1) % 3
            PublicDataset.objects.filter(pk=dataset.pk).update(
                download_turn=dataset.download_turn
            )
            attempted += 1
            PublicArchiveFile.objects.filter(pk=record.pk).update(last_attempt_at=now())
            try:
                amount = _download_file(
                    record,
                    minimum_free,
                    budget_bytes=max_bytes - transferred,
                    deadline=deadline,
                )
                downloaded += 1
                transferred += amount
                stored += amount
                PublicDataset.objects.filter(pk=dataset.pk).update(
                    last_sync_at=now(), last_error=""
                )
            except ArchiveLockLost:
                raise
            except Exception as exc:
                transferred += getattr(exc, "archive_bytes", 0)
                failures = record.failure_count + 1
                delay = max(
                    _pause_for_rate_limit(exc), min(86400, 900 * 2 ** min(failures - 1, 7))
                )
                message = f"{type(exc).__name__}: {exc}"[:1000]
                PublicArchiveFile.objects.filter(pk=record.pk).update(
                    status=PublicArchiveFile.Status.FAILED,
                    failure_count=failures,
                    retry_at=now() + timedelta(seconds=delay),
                    last_checked_at=now(),
                    last_error=message,
                )
                PublicDataset.objects.filter(pk=dataset.pk).update(last_error=message)
                if len(errors) < 25:
                    errors.append({"dataset": dataset.slug, "error": message})
                if isinstance(exc, StoragePaused):
                    blocked = "minimum_free_space"
                    break
        if blocked or not progressed:
            break
    oversized = PublicArchiveFile.objects.filter(
        dataset__enabled=True, status__in=("PENDING", "FAILED"), remote_size__gt=max_bytes
    ).count()
    return {
        "enabled": True,
        "attempted_files": attempted,
        "downloaded_files": downloaded,
        "downloaded_bytes": stored,
        "transferred_bytes": transferred,
        "oversized_files": oversized,
        "blocked": blocked,
        "errors": errors,
        "catalog": catalog,
    }


def verify_archive_files(*, limit=500):
    checked = missing = 0
    for record in PublicArchiveFile.objects.filter(
        status=PublicArchiveFile.Status.STORED
    ).order_by("last_checked_at", "pk")[:limit]:
        target = archive_root() / record.relative_path
        valid = target.is_file() and target.stat().st_size == record.stored_bytes
        updates = {"last_checked_at": now()}
        if not valid:
            missing += 1
            updates.update(
                status=PublicArchiveFile.Status.PENDING,
                retry_at=None,
                last_error="Stored file missing or size changed.",
            )
        PublicArchiveFile.objects.filter(pk=record.pk).update(**updates)
        checked += 1
    return {"checked": checked, "missing": missing, "errors": []}
