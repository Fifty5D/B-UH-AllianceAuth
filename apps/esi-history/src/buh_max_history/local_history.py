"""Archive business records from installed Auth apps, excluding credential tables."""

import json
import logging
import re
import time
from datetime import timedelta
from types import SimpleNamespace

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.utils.timezone import now

from .capture import _json_safe, archive_esi_result
from .collection import target_key
from .models import ArchiveCollectionTarget, ArchiveConfiguration
from .job_lock import ArchiveLockLost, assert_public_archive_lock

logger = logging.getLogger(__name__)
BUSINESS_APPS = frozenset(
    {"memberaudit", "structures", "moonmining", "buh_moon_tax", "buh_structure_ops"}
)
SENSITIVE_FIELD = re.compile(
    r"token|password|secret|credential|session|authorization|private_key|access_key", re.I
)


def eligible(model):
    meta = model._meta
    return (
        meta.app_label in BUSINESS_APPS
        and meta.managed
        and not meta.proxy
        and meta.pk.get_internal_type()
        in {
            "AutoField",
            "BigAutoField",
            "SmallAutoField",
            "IntegerField",
            "SmallIntegerField",
            "PositiveSmallIntegerField",
            "BigIntegerField",
            "PositiveIntegerField",
            "PositiveBigIntegerField",
        }
    )


def model_fields(model):
    return [
        field.attname
        for field in model._meta.concrete_fields
        if not SENSITIVE_FIELD.search(field.attname)
    ]


def capture_record(model, values, *, deleted=False):
    config = ArchiveConfiguration.objects.filter(singleton_id=1).first()
    if config is None:
        if not getattr(settings, "BUH_ESI_ARCHIVE_CAPTURE_ENABLED", True):
            return None
        config = ArchiveConfiguration.get_solo()
    if not (
        config.local_history_enabled
        and config.capture_enabled
        and config.capture_private_esi
    ):
        return None
    label = model._meta.label_lower
    parameters = {"model": label, "record_id": values[model._meta.pk.attname]}
    operation = SimpleNamespace(
        token=SimpleNamespace(character_id=None, user_id=None),
        method="GET",
        url=f"auth://{label}/{{record_id}}",
        _kwargs=parameters,
        operation=SimpleNamespace(operationId=f"auth:{label}"),
        api=SimpleNamespace(app_name="B-UH Auth business history"),
    )
    payload = {"deleted": deleted, "fields": _json_safe(values)}
    body = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    response = SimpleNamespace(
        status_code=200, content=body, headers={"content-type": "application/json"}
    )
    return archive_esi_result(operation, payload, response)


def record_change(sender, instance, *, deleted=False, raw=False, **kwargs):
    if raw or not eligible(sender):
        return
    # Copy values before deletion or subsequent mutation. Do not traverse related
    # objects, collect credentials, or archive a transaction which rolls back.
    values = {field: getattr(instance, field) for field in model_fields(sender)}

    def after_commit():
        try:
            capture_record(sender, values, deleted=deleted)
        except Exception as exc:
            # Business writes always succeed independently of the archive.
            logger.warning(
                "Business history capture deferred for %s (%s); the periodic scan can recover current rows.",
                sender._meta.label_lower,
                type(exc).__name__,
            )

    transaction.on_commit(after_commit)


def record_delete(sender, instance, **kwargs):
    record_change(sender, instance, deleted=True, **kwargs)


def install_business_history_hooks():
    post_save.connect(record_change, dispatch_uid="buh_history_business_save", weak=False)
    post_delete.connect(
        record_delete, dispatch_uid="buh_history_business_delete", weak=False
    )


def collect_local_batch(*, deadline=None):
    config = ArchiveConfiguration.get_solo()
    if not (
        config.local_history_enabled
        and config.capture_enabled
        and config.capture_private_esi
    ):
        return {"paused": "disabled"}
    deadline = deadline or time.monotonic() + 60
    models = {
        model._meta.label_lower: model for model in apps.get_models() if eligible(model)
    }
    ArchiveCollectionTarget.objects.bulk_create(
        [
            ArchiveCollectionTarget(
                key=target_key("auth", label, {}), kind="auth", operation_id=label
            )
            for label in models
        ],
        ignore_conflicts=True,
    )
    captured = 0
    targets = list(
        ArchiveCollectionTarget.objects.filter(kind="auth", next_attempt_at__lte=now())
        .exclude(status="disabled")
        .order_by("last_attempt_at", "pk")[:100]
    )
    # Every eligible model gets a small turn; large mining/mail tables cannot
    # consume the entire scan budget before other business records are seen.
    for target in targets:
        if captured >= config.local_rows_per_run or time.monotonic() >= deadline:
            break
        model = models.get(target.operation_id)
        if model is None:
            continue
        assert_public_archive_lock("collection")
        target.last_attempt_at = now()
        limit = min(25, config.local_rows_per_run - captured)
        try:
            rows = list(
                model.objects.filter(pk__gt=target.cursor.get("pk", 0))
                .order_by("pk")
                .values(*model_fields(model))[:limit]
            )
            processed = 0
            for row in rows:
                if time.monotonic() >= deadline:
                    break
                assert_public_archive_lock("collection")
                snapshot = capture_record(model, row)
                if snapshot is None:
                    target.status, target.detail = (
                        "storage_paused",
                        "Business record was not saved; inspect capture guards.",
                    )
                    target.save()
                    return {"captured": captured, "paused": "storage"}
                target.cursor = {"pk": row[model._meta.pk.attname]}
                target.last_success_at = now()
                target.save()
                captured += 1
                processed += 1
            if processed == len(rows) and len(rows) < limit:
                target.cursor = {}
                target.status = "current"
                target.completed_at = now()
                target.next_attempt_at = now() + timedelta(hours=1)
            else:
                target.status = "backlog"
                target.next_attempt_at = now()
            target.detail = ""
            target.failure_count = 0
        except ArchiveLockLost:
            raise
        except Exception as exc:
            target.status = "failed"
            target.failure_count += 1
            target.detail = type(exc).__name__
            target.next_attempt_at = now() + timedelta(minutes=15)
        target.save()
    return {"captured": captured, "models": len(models)}
