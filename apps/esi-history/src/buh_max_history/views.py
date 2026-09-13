from __future__ import annotations

import shutil
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import models
from django.db.models import Count, Sum
from django.db.models.functions import Coalesce
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from . import __version__
from .access import has_app_access, has_permission, permission_map
from .capture import archive_root
from .models import (
    ArchiveCaptureIssue,
    ArchiveConfiguration,
    ArchiveJob,
    ArchiveSnapshot,
    ArchiveStream,
    PublicArchiveFile,
    PublicDataset,
)
from .tasks import run_archive_job


def _require(user, codename):
    if not has_permission(user, codename):
        raise PermissionDenied


def _safe_archive_path(relative_path: str) -> Path:
    root = archive_root().resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise Http404("Archived file path is invalid.") from exc
    if not candidate.is_file():
        raise Http404("Archived file is unavailable.")
    return candidate


def _job_payload(job: ArchiveJob):
    return {
        "id": str(job.pk),
        "kind": job.kind,
        "kind_label": job.get_kind_display(),
        "status": job.status,
        "status_label": job.get_status_display(),
        "message": job.message,
        "result": job.result,
        "requested_at": job.requested_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@login_required
def dashboard(request):
    if not has_app_access(request.user):
        raise PermissionDenied
    permissions = permission_map(request.user)
    config = ArchiveConfiguration.get_solo()
    streams = ArchiveStream.objects.all()
    if not permissions["view_private_archive_metadata"]:
        streams = streams.filter(is_private=False)
    streams = list(streams[:30])
    for stream in streams:
        stream.latest_snapshot = stream.snapshots.filter(
            payload_sha256=stream.current_payload_sha256
        ).first()

    totals = {}
    disk = None
    if permissions["view_archive_storage"]:
        totals = ArchiveStream.objects.aggregate(
            streams=Count("pk"),
            requests=Coalesce(Sum("request_count"), 0),
            snapshots=Coalesce(Sum("snapshot_count"), 0),
            source_bytes=Coalesce(Sum("source_bytes"), 0),
            stored_bytes=Coalesce(Sum("stored_bytes"), 0),
            skipped=Coalesce(Sum("skipped_count"), 0),
        )
        public = PublicArchiveFile.objects.aggregate(
            files=Count("pk"),
            stored_bytes=Coalesce(
                Sum(
                    "stored_bytes", filter=models.Q(status=PublicArchiveFile.Status.STORED)
                ),
                0,
            ),
            remote_bytes=Coalesce(Sum("remote_size"), 0),
        )
        totals.update(
            public_files=public["files"],
            public_stored_bytes=public["stored_bytes"],
            public_remote_bytes=public["remote_bytes"],
        )
        root = archive_root()
        root.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(root)

    datasets = ()
    if permissions["view_public_archive"]:
        datasets = PublicDataset.objects.annotate(
            file_count=Count("files"),
            stored_count=Count(
                "files", filter=models.Q(files__status=PublicArchiveFile.Status.STORED)
            ),
            stored_size=Coalesce(
                Sum(
                    "files__stored_bytes",
                    filter=models.Q(files__status=PublicArchiveFile.Status.STORED),
                ),
                0,
            ),
        )

    issues = ()
    if permissions["manage_archive_settings"]:
        issues = ArchiveCaptureIssue.objects.all()[:10]

    return render(
        request,
        "buh_max_history/dashboard.html",
        {
            "app_name": "ESI History Archive",
            "app_version": __version__,
            "permissions": permissions,
            "config": config,
            "totals": totals,
            "disk": disk,
            "streams": streams,
            "datasets": datasets,
            "jobs": ArchiveJob.objects.select_related("requested_by")[:10],
            "issues": issues,
        },
    )


@require_POST
@login_required
def start_job(request, kind):
    _require(request.user, "run_public_archive_sync")
    kinds = {
        "catalog": ArchiveJob.Kind.CATALOG,
        "sync": ArchiveJob.Kind.SYNC,
        "verify": ArchiveJob.Kind.VERIFY,
    }
    if kind not in kinds:
        return JsonResponse({"ok": False, "error": "Unknown archive job."}, status=404)
    job = ArchiveJob.objects.create(
        kind=kinds[kind],
        requested_by=request.user,
        message="Waiting for an Alliance Auth worker.",
    )
    try:
        run_archive_job.delay(str(job.pk))
    except Exception:
        from django.utils.timezone import now

        ArchiveJob.objects.filter(pk=job.pk).update(
            status=ArchiveJob.Status.FAILED,
            finished_at=now(),
            message="Worker queue unavailable; the scheduled collector can retry.",
        )
        return JsonResponse({"ok": False, "error": "Worker queue unavailable."}, status=503)
    return JsonResponse({"ok": True, "job": _job_payload(job)}, status=202)


@require_GET
@login_required
def job_detail(request, job_id):
    _require(request.user, "run_public_archive_sync")
    try:
        job = ArchiveJob.objects.get(pk=job_id)
    except ArchiveJob.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Archive job not found."}, status=404)
    return JsonResponse({"ok": True, "job": _job_payload(job)})


@require_GET
@login_required
def download_snapshot(request, snapshot_id):
    try:
        snapshot = ArchiveSnapshot.objects.select_related("stream").get(pk=snapshot_id)
    except ArchiveSnapshot.DoesNotExist as exc:
        raise Http404("Archive snapshot not found.") from exc
    permission = (
        "download_private_archive"
        if snapshot.stream.is_private
        else "download_public_archive"
    )
    _require(request.user, permission)
    path = _safe_archive_path(snapshot.relative_path)
    return FileResponse(
        path.open("rb"),
        as_attachment=True,
        filename=path.name,
        content_type="application/gzip",
    )


@require_GET
@login_required
def download_public_file(request, file_id):
    _require(request.user, "download_public_archive")
    try:
        record = PublicArchiveFile.objects.get(pk=file_id)
    except PublicArchiveFile.DoesNotExist as exc:
        raise Http404("Public archive file not found.") from exc
    path = _safe_archive_path(record.relative_path)
    return FileResponse(path.open("rb"), as_attachment=True, filename=path.name)
