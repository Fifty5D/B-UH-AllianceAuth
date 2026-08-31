"""Celery orchestration for source refresh, reconciliation, and scheduled audits."""

import datetime as dt

from celery import shared_task
from django.utils.timezone import now
from memberaudit import tasks as memberaudit_tasks
from memberaudit.models import Character
from moonmining import tasks as moonmining_tasks

from . import app_settings
from .auditor import reconcile
from .models import AuditRun, TaxConfiguration

ACTIVE_STATUSES = (
    AuditRun.Status.QUEUED,
    AuditRun.Status.REFRESHING,
    AuditRun.Status.RUNNING,
)


def expire_stale_audits() -> int:
    """Fail abandoned runs so a worker restart cannot block audits forever."""

    cutoff = now() - dt.timedelta(hours=app_settings.STALE_AUDIT_HOURS)
    return AuditRun.objects.filter(
        status__in=ACTIVE_STATUSES,
        queued_at__lt=cutoff,
    ).update(
        status=AuditRun.Status.FAILED,
        error=(
            "Audit exceeded the guarded runtime and was released automatically. "
            "Start a new audit; stored source and billing data were preserved."
        ),
        finished_at=now(),
    )


def create_audit(trigger: str, requested_by=None) -> AuditRun:
    return AuditRun.objects.create(trigger=trigger, requested_by=requested_by)


@shared_task
def run_scheduled_audit():
    """Create one scheduled audit when the editable database interval is due."""

    expire_stale_audits()
    existing = AuditRun.objects.filter(
        status__in=ACTIVE_STATUSES,
    ).first()
    if existing:
        return existing.pk
    config, _ = TaxConfiguration.objects.get_or_create(singleton_id=1)
    due_cutoff = now() - dt.timedelta(hours=config.audit_interval_hours)
    recent = (
        AuditRun.objects.exclude(status=AuditRun.Status.FAILED)
        .filter(queued_at__gte=due_cutoff)
        .order_by("-queued_at")
        .first()
    )
    if recent:
        return recent.pk
    audit = create_audit(AuditRun.Trigger.SCHEDULED)
    refresh_sources.delay(audit.pk)
    return audit.pk


@shared_task
def refresh_sources(audit_run_id: int):
    """Queue upstream ESI refreshes, then allow them time to settle."""

    audit = AuditRun.objects.get(pk=audit_run_id)
    audit.status = AuditRun.Status.REFRESHING
    audit.started_at = audit.started_at or now()
    audit.source_refresh_requested_at = now()
    audit.save(
        update_fields=("status", "started_at", "source_refresh_requested_at")
    )

    try:
        moonmining_tasks.run_report_updates.delay()
        characters = Character.objects.filter(is_disabled=False).order_by("pk")
        total_characters = characters.count()
        character_ids = list(
            characters.values_list("pk", flat=True)[
                : app_settings.MAX_REFRESH_CHARACTERS
            ]
        )
        if total_characters > len(character_ids):
            audit.warnings = list(audit.warnings or []) + [
                (
                    f"Queued {len(character_ids)} of {total_characters} Member Audit "
                    "characters because BUH_MOON_TAX_MAX_REFRESH_CHARACTERS was reached."
                )
            ]
            audit.save(update_fields=("warnings",))
        for character_pk in character_ids:
            memberaudit_tasks.update_character_wallet_journal.delay(
                character_pk, False
            )
            memberaudit_tasks.update_character_contracts.delay(character_pk, False)

        execute_audit.apply_async(
            kwargs={"audit_run_id": audit.pk},
            countdown=app_settings.SOURCE_SETTLE_SECONDS,
        )
    except Exception as exc:
        audit.status = AuditRun.Status.FAILED
        audit.error = f"Could not queue source refreshes: {exc}"
        audit.finished_at = now()
        audit.save(update_fields=("status", "error", "finished_at"))
        raise
    return {
        "audit_run_id": audit.pk,
        "characters_queued": len(character_ids),
        "characters_available": total_characters,
        "settle_seconds": app_settings.SOURCE_SETTLE_SECONDS,
    }


@shared_task
def execute_audit(audit_run_id: int):
    audit = AuditRun.objects.get(pk=audit_run_id)
    try:
        return reconcile(audit)
    except Exception as exc:
        audit.status = AuditRun.Status.FAILED
        audit.error = str(exc)
        audit.finished_at = now()
        audit.save(update_fields=("status", "error", "finished_at"))
        raise
