"""Collect archive code and bounded, read-only database metadata; never run jobs."""

import ast
import base64
import contextlib
import datetime
import hashlib
import importlib.metadata as metadata
import io
import json
import logging
import os
from pathlib import Path
import re
import signal
import sys
import zipfile

report = {
    "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "purpose": "ESI archive audit; no collection, repair, cleanup or deployment",
    "errors": {},
    "source": {},
    "tables": {},
}
bundle = io.BytesIO()
archive = zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED)
logging.disable(logging.CRITICAL)


class AuditDeadline(BaseException):
    pass


signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(AuditDeadline()))
signal.alarm(150)


def probe(name, fn):
    try:
        return fn()
    except Exception as exc:
        report["errors"][name] = type(exc).__name__
        return None


def collect_source():
    dist = metadata.distribution("aa-buh-max-history")
    report["version"] = dist.version
    total = 0
    for entry in dist.files or []:
        parts = entry.parts
        if not parts or ".." in parts or entry.is_absolute():
            continue
        if not parts[0].startswith("buh") or entry.suffix not in {
            ".py",
            ".js",
            ".css",
            ".html",
        }:
            continue
        path = Path(dist.locate_file(entry))
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            continue
        data = path.read_bytes()
        total += len(data)
        if total > 8 * 1024 * 1024:
            raise ValueError("Source size limit")
        archive.writestr("source/" + entry.as_posix(), data)
        report["source"][entry.as_posix()] = hashlib.sha256(data).hexdigest()
    if not report["source"]:
        raise ValueError("No installed source")


def collect_database():
    manage = Path("/home/allianceauth/myauth/manage.py")
    sys.path.insert(0, str(manage.parent))
    for node in ast.walk(ast.parse(manage.read_text())):
        if (
            isinstance(node, ast.Call)
            and len(node.args) == 2
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault"
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "DJANGO_SETTINGS_MODULE"
            and isinstance(node.args[1], ast.Constant)
        ):
            os.environ.setdefault("DJANGO_SETTINGS_MODULE", node.args[1].value)
    from django.db.backends.signals import connection_created

    def read_only(sender, connection, **kwargs):
        with connection.cursor() as cursor:
            if connection.vendor == "mysql":
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute("SET SESSION max_statement_time=3")
            elif connection.vendor == "sqlite":
                cursor.execute("PRAGMA query_only=ON")
            elif connection.vendor == "postgresql":
                cursor.execute("SET default_transaction_read_only=on")
                cursor.execute("SET statement_timeout=3000")
            else:
                raise RuntimeError("Unsupported read-only database")

    connection_created.connect(read_only, weak=False)
    import django

    django.setup()
    from django.apps import apps
    from django.conf import settings
    from django.db import connection

    archive_apps = [
        a for a in apps.get_app_configs() if "buh" in a.name and "history" in a.name
    ]
    report["archive_apps"] = [{"name": a.name, "label": a.label} for a in archive_apps]
    if not archive_apps:
        raise RuntimeError("Archive app is not registered in these settings")
    report["scheduler"] = str(getattr(settings, "CELERY_BEAT_SCHEDULER", "default"))
    report["configured_schedules"] = []
    for item in getattr(settings, "CELERY_BEAT_SCHEDULE", {}).values():
        schedule = item.get("schedule")
        report["configured_schedules"].append(
            {
                "task": item.get("task"),
                "schedule": str(schedule),
                "seconds": schedule.total_seconds()
                if isinstance(schedule, datetime.timedelta)
                else None,
            }
        )
    report["archive_settings"] = {
        key: getattr(settings, key)
        for key in dir(settings)
        if key.startswith(
            ("BUH_MAX_HISTORY", "BUH_HISTORY", "BUH_ESI_ARCHIVE", "MEMBERAUDIT")
        )
        and not re.search("SECRET|TOKEN|PASSWORD|KEY|CREDENTIAL", key)
        and (
            getattr(settings, key) is None
            or isinstance(getattr(settings, key), (bool, int, float))
        )
    }
    from memberaudit import app_settings as memberaudit_settings

    report["memberaudit_effective_settings"] = {
        "retention_days": memberaudit_settings.MEMBERAUDIT_DATA_RETENTION_LIMIT,
        "max_mails": memberaudit_settings.MEMBERAUDIT_MAX_MAILS,
        "default_stale_minutes": memberaudit_settings.MEMBERAUDIT_SECTION_STALE_MINUTES_GLOBAL_DEFAULT,
        "section_stale_minutes": {
            **memberaudit_settings.MEMBERAUDIT_SECTION_STALE_MINUTES_SECTION_DEFAULTS,
            **memberaudit_settings.MEMBERAUDIT_SECTION_STALE_MINUTES_CONFIG,
        },
    }
    from celery import current_app

    report["celery_scheduler"] = str(current_app.conf.beat_scheduler)
    report["celery_schedules"] = [
        {"task": v.get("task"), "schedule": str(v.get("schedule"))}
        for v in (current_app.conf.beat_schedule or {}).values()
    ]
    quote = connection.ops.quote_name
    safe_names = set(
        "id name slug key task kind status state operation operation_id endpoint method scope "
        "enabled active is_private capture_private capture_public dataset dataset_id interval_id crontab_id "
        "period every minute hour day_of_week day_of_month month_of_year timezone total_run_count one_off "
        "requests request_count changes change_count snapshot_count file_count files_total files_done "
        "stored_count downloaded_count cataloged_count attempts retry_count bytes_downloaded bytes_stored "
        "stored_bytes compressed_bytes raw_bytes size size_bytes observed_bytes total_bytes min_free_bytes "
        "max_files max_bytes batch_size batch_files batch_bytes retention_days reason occurrences skipped_count source_bytes failure_count".split()
    )

    def query(sql):
        with connection.cursor() as cursor:
            cursor.execute(sql)
            names = [col[0] for col in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchmany(501)]

    models = [m for app in archive_apps for m in app.get_models()]
    if apps.is_installed("django_celery_beat"):
        models += [
            apps.get_model("django_celery_beat", name)
            for name in ("PeriodicTask", "IntervalSchedule", "CrontabSchedule")
        ]
    for model in models:
        if not model._meta.managed or model._meta.proxy:
            continue
        name, table = model._meta.label, quote(model._meta.db_table)
        fields = [f for f in model._meta.concrete_fields]
        data = report["tables"][name] = {
            "schema": {f.name: f.get_internal_type() for f in fields}
        }
        data["row_count"] = probe(
            name + ":count", lambda: query("SELECT COUNT(*) AS n FROM " + table)
        )
        selected = [
            f
            for f in fields
            if (
                f.name in safe_names
                or f.attname in safe_names
                or (
                    any(part in model._meta.model_name for part in ("config", "setting"))
                    and f.get_internal_type()
                    in {
                        "SmallIntegerField",
                        "PositiveSmallIntegerField",
                        "IntegerField",
                        "PositiveIntegerField",
                        "BigIntegerField",
                        "PositiveBigIntegerField",
                        "FloatField",
                        "DecimalField",
                    }
                )
                or f.get_internal_type() in {"DateTimeField", "DateField", "BooleanField"}
            )
            and f.get_internal_type() not in {"JSONField", "BinaryField", "TextField"}
        ]
        if selected:
            columns = ", ".join(quote(f.column) for f in selected)
            data["recent_metadata"] = probe(
                name + ":recent",
                lambda: query(
                    "SELECT "
                    + columns
                    + " FROM "
                    + table
                    + " ORDER BY "
                    + quote(model._meta.pk.column)
                    + " DESC LIMIT 101"
                ),
            )
            data["recent_metadata_limit"] = 100
            if data["recent_metadata"] and len(data["recent_metadata"]) > 100:
                data["recent_metadata_truncated"] = True
                data["recent_metadata"] = data["recent_metadata"][:100]
        for field in fields:
            if field.name not in {
                "status",
                "state",
                "operation",
                "operation_id",
                "dataset",
                "dataset_id",
            }:
                continue
            col = quote(field.column)
            sums = ["COUNT(*) AS rows_total"]
            sums += [
                "MAX(" + quote(f.column) + ") AS " + quote("latest_" + f.name)
                for f in fields
                if f.get_internal_type() in {"DateTimeField", "DateField"}
            ]
            sums += [
                "SUM(" + quote(f.column) + ") AS " + quote("sum_" + f.name)
                for f in fields
                if f.name in safe_names
                and any(s in f.name for s in ("bytes", "count"))
                and f.get_internal_type()
                in {
                    "SmallIntegerField",
                    "PositiveSmallIntegerField",
                    "IntegerField",
                    "BigIntegerField",
                    "PositiveIntegerField",
                    "PositiveBigIntegerField",
                }
            ]
            data["by_" + field.name] = probe(
                name + ":group:" + field.name,
                lambda: query(
                    "SELECT "
                    + col
                    + ", "
                    + ", ".join(sums)
                    + " FROM "
                    + table
                    + " GROUP BY "
                    + col
                    + " LIMIT 501"
                ),
            )
            data["by_" + field.name + "_limit"] = 500
            if data["by_" + field.name] and len(data["by_" + field.name]) > 500:
                data["by_" + field.name + "_truncated"] = True
                data["by_" + field.name] = data["by_" + field.name][:500]
    connection.close()


try:
    with (
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        probe("source", collect_source)
        probe("database", collect_database)
except AuditDeadline:
    report["errors"]["deadline"] = "Audit stopped at 150 seconds; partial evidence retained"
signal.alarm(0)
archive.writestr("audit.json", json.dumps(report, default=str, indent=2))
archive.close()
print("BUH_ESI_AUDIT:" + base64.b64encode(bundle.getvalue()).decode("ascii"))
