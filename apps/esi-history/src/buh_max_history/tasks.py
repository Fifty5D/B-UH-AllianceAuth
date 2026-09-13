"""Bounded archive work with automatic recovery after worker termination."""

from datetime import timedelta

from celery import shared_task
from django.utils.timezone import now

from .job_lock import public_archive_lock
from .models import ArchiveConfiguration, ArchiveJob, PublicArchiveFile
from .public_archive import (
    catalog_enabled_datasets,
    sync_public_archive,
    verify_archive_files,
)

# Checkpoints stop work earlier. A hard limit also releases the kernel lock if
# an external service or database driver stops responding.
TASK_LIMITS = {"soft_time_limit": 1800, "time_limit": 1860}


def _run_job(job, *, catalog_first=True):
    with public_archive_lock() as acquired:
        if not acquired:
            ArchiveJob.objects.filter(pk=job.pk, status=ArchiveJob.Status.QUEUED).update(
                status=ArchiveJob.Status.FAILED,
                message="Another public archive operation owns the lock.",
                finished_at=now(),
            )
            return {"ok": True, "skipped": "another_archive_job_active"}
        # Lock ownership, not age, proves no other writer is alive.
        ArchiveJob.objects.filter(status=ArchiveJob.Status.RUNNING).exclude(
            pk=job.pk
        ).update(
            status=ArchiveJob.Status.FAILED,
            message="Worker stopped before completion; unfinished work will resume.",
            finished_at=now(),
        )
        ArchiveJob.objects.filter(
            status=ArchiveJob.Status.QUEUED,
            requested_at__lt=now() - timedelta(hours=6),
        ).exclude(pk=job.pk).update(
            status=ArchiveJob.Status.FAILED,
            message="Queued request expired; scheduled collection can resume.",
            finished_at=now(),
        )
        claimed = ArchiveJob.objects.filter(
            pk=job.pk, status=ArchiveJob.Status.QUEUED
        ).update(status=ArchiveJob.Status.RUNNING, started_at=now())
        if not claimed:
            return {"ok": True, "skipped": "job_already_started"}
        PublicArchiveFile.objects.filter(
            status=PublicArchiveFile.Status.DOWNLOADING
        ).update(status=PublicArchiveFile.Status.PENDING, retry_at=None)
        try:
            if job.kind == ArchiveJob.Kind.CATALOG:
                result = catalog_enabled_datasets()
            elif job.kind == ArchiveJob.Kind.SYNC:
                result = sync_public_archive(catalog_first=catalog_first)
            elif job.kind == ArchiveJob.Kind.VERIFY:
                result = verify_archive_files()
            else:
                raise ValueError("Unknown archive operation.")
            failed = bool(result.get("errors") or result.get("blocked"))
            ArchiveJob.objects.filter(pk=job.pk).update(
                status=ArchiveJob.Status.FAILED if failed else ArchiveJob.Status.SUCCEEDED,
                result=result,
                message=(
                    "Collection made partial progress; see retry or storage details."
                    if failed
                    else "Archive batch completed; remaining work stays queued."
                ),
                finished_at=now(),
            )
            return {"ok": not failed, "job": str(job.pk), "result": result}
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:4000]
            ArchiveJob.objects.filter(pk=job.pk).update(
                status=ArchiveJob.Status.FAILED, message=message, finished_at=now()
            )
            return {"ok": False, "job": str(job.pk), "error": message}


@shared_task(**TASK_LIMITS)
def run_archive_job(job_id):
    job = ArchiveJob.objects.filter(pk=job_id).first()
    if job is None:
        return {"ok": False, "error": "Archive job not found."}
    return _run_job(job)


@shared_task(**TASK_LIMITS)
def scheduled_public_archive_sync():
    if not ArchiveConfiguration.get_solo().public_mirror_enabled:
        return {"ok": True, "skipped": "public_mirror_disabled"}
    # A stale database row must never disable the recurring scheduler.
    return _run_job(ArchiveJob.objects.create(kind=ArchiveJob.Kind.SYNC))
