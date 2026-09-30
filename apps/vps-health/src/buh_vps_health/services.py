import math
from datetime import timedelta

from allianceauth.notifications import notify
from django.utils.dateparse import parse_datetime
from django.utils.timezone import now

from .access import notification_recipients
from .agent_client import AgentUnavailable, action_status, get_metrics
from .models import (
    AlertState,
    HealthConfiguration,
    MetricSnapshot,
    OperationalAction,
)
from .workload import get_worker_workload


def _number(mapping, key, default=0):
    value = mapping.get(key, default)
    return default if value is None else value


def capture_snapshot(metrics=None, workload=None):
    metrics = metrics or get_metrics()
    workload = workload or get_worker_workload(force=True)
    host = metrics.get("host") or {}
    docker = metrics.get("docker") or {}
    workers = metrics.get("workers") or {}
    snapshot = MetricSnapshot.objects.create(
        cpu_percent=_number(host, "cpu_percent"),
        cpu_count=max(1, int(_number(host, "cpu_count", 1))),
        load_1=_number(host, "load_1"),
        load_5=_number(host, "load_5"),
        load_15=_number(host, "load_15"),
        memory_percent=_number(host, "memory_percent"),
        memory_used_bytes=int(_number(host, "memory_used_bytes")),
        memory_total_bytes=int(_number(host, "memory_total_bytes")),
        swap_percent=_number(host, "swap_percent"),
        disk_percent=_number(host, "disk_percent"),
        disk_used_bytes=int(_number(host, "disk_used_bytes")),
        disk_total_bytes=int(_number(host, "disk_total_bytes")),
        network_rx_bytes_per_second=_number(host, "network_rx_bytes_per_second"),
        network_tx_bytes_per_second=_number(host, "network_tx_bytes_per_second"),
        container_total=int(_number(docker, "total")),
        container_unhealthy=int(_number(docker, "unhealthy")),
        worker_desired=int(_number(workers, "desired")),
        worker_online=int(_number(workers, "online")),
        active_tasks=int(_number(workload, "active_total")),
        queued_tasks=int(_number(workload, "queued")),
    )
    return snapshot


def _sustained(field, threshold, samples):
    values = list(
        MetricSnapshot.objects.order_by("-captured_at").values_list(field, flat=True)[
            :samples
        ]
    )
    return len(values) >= samples and all(
        float(value) >= float(threshold) for value in values
    )


def _set_alert(key, *, active, title, message, severity="warning"):
    current = now()
    alert, created = AlertState.objects.get_or_create(
        key=key,
        defaults={
            "active": active,
            "title": title,
            "message": message,
            "severity": severity,
            "resolved_at": None if active else current,
        },
    )
    transitioned = (created and active) or alert.active != active
    alert.title = title
    alert.message = message
    alert.severity = severity if active else AlertState.Severity.SUCCESS
    alert.active = active
    if created and not active:
        # A healthy first sample is baseline state, not a recovery event.
        alert.notification_sent_at = current
    elif transitioned:
        alert.notification_sent_at = None
        alert.resolved_at = None if active else current
    alert.save()
    return alert, transitioned


def evaluate_alerts(snapshot, config=None):
    config = config or HealthConfiguration.get_solo()
    samples = max(
        1,
        math.ceil(config.alert_sustain_minutes / config.snapshot_interval_minutes),
    )
    checks = (
        (
            "cpu",
            _sustained("cpu_percent", config.warning_cpu_percent, samples),
            "Sustained VPS CPU pressure",
            f"CPU remained at or above {config.warning_cpu_percent}% for approximately {config.alert_sustain_minutes} minutes.",
        ),
        (
            "memory",
            _sustained("memory_percent", config.warning_memory_percent, samples),
            "Sustained VPS memory pressure",
            f"RAM remained at or above {config.warning_memory_percent}% for approximately {config.alert_sustain_minutes} minutes.",
        ),
        (
            "disk",
            _sustained("disk_percent", config.warning_disk_percent, samples),
            "VPS disk space warning",
            f"Disk utilization is at or above {config.warning_disk_percent}%.",
        ),
        (
            "load",
            _sustained(
                "load_5",
                float(config.warning_load_per_cpu) * max(1, snapshot.cpu_count),
                samples,
            ),
            "Sustained VPS load warning",
            "Five-minute load remained above the configured per-CPU threshold.",
        ),
        (
            "queue",
            _sustained("queued_tasks", config.warning_queue_depth, samples),
            "Alliance Auth task queue warning",
            f"The Celery queue remained at or above {config.warning_queue_depth} tasks.",
        ),
        (
            "containers",
            snapshot.container_unhealthy > 0,
            "Unhealthy or restarting Docker container",
            f"{snapshot.container_unhealthy} container(s) are unhealthy or restarting.",
        ),
    )
    transitioned = []
    for key, active, title, message in checks:
        alert, changed = _set_alert(
            key, active=active, title=title, message=message, severity="danger"
        )
        if changed:
            transitioned.append(alert)
    return transitioned


def record_agent_failure(message):
    return _set_alert(
        "agent",
        active=True,
        title="VPS Health host agent unavailable",
        message=message,
        severity="danger",
    )[0]


def record_agent_recovery():
    # Do not create a synthetic recovery on the first successful collection.
    if not AlertState.objects.filter(key="agent").exists():
        return None
    return _set_alert(
        "agent",
        active=False,
        title="VPS Health host agent recovered",
        message="Live host metrics and guarded controls are responding again.",
    )[0]


def dispatch_notifications(config=None):
    config = config or HealthConfiguration.get_solo()
    if not config.notify_directors:
        return 0
    recipients = list(notification_recipients())
    sent = 0
    for alert in AlertState.objects.filter(notification_sent_at__isnull=True).order_by(
        "last_seen_at"
    ):
        for user in recipients:
            notify(
                user=user,
                title=alert.title,
                message=alert.message,
                level=alert.severity,
            )
            sent += 1
        alert.notification_sent_at = now()
        alert.save(update_fields=("notification_sent_at", "last_seen_at"))
    return sent


def cleanup_history(config=None):
    config = config or HealthConfiguration.get_solo()
    cutoff = now() - timedelta(days=config.history_retention_days)
    deleted, _ = MetricSnapshot.objects.filter(captured_at__lt=cutoff).delete()
    return deleted


def capture_and_evaluate():
    config = HealthConfiguration.get_solo()
    try:
        metrics = get_metrics()
        workload = get_worker_workload(force=True)
        snapshot = capture_snapshot(metrics, workload)
        record_agent_recovery()
        evaluate_alerts(snapshot, config)
    except AgentUnavailable as exc:
        record_agent_failure(str(exc))
        dispatch_notifications(config)
        return {"ok": False, "error": str(exc)}
    notifications = dispatch_notifications(config)
    deleted = cleanup_history(config)
    return {
        "ok": True,
        "snapshot": snapshot.pk,
        "notifications": notifications,
        "history_deleted": deleted,
    }


STATUS_MAP = {
    "accepted": OperationalAction.Status.ACCEPTED,
    "queued": OperationalAction.Status.ACCEPTED,
    "running": OperationalAction.Status.RUNNING,
    "succeeded": OperationalAction.Status.SUCCEEDED,
    "failed": OperationalAction.Status.FAILED,
}


def synchronize_action(action):
    if not action.agent_action_id or action.status in {
        OperationalAction.Status.SUCCEEDED,
        OperationalAction.Status.FAILED,
    }:
        return action
    response = action_status(action.agent_action_id)
    data = response.get("action") or {}
    action.status = STATUS_MAP.get(data.get("status"), action.status)
    action.message = data.get("message") or action.message
    action.result_payload = data.get("result") or action.result_payload
    if data.get("started_at"):
        action.started_at = parse_datetime(data["started_at"])
    if data.get("finished_at"):
        action.finished_at = parse_datetime(data["finished_at"])
    action.save(
        update_fields=(
            "status",
            "message",
            "result_payload",
            "started_at",
            "finished_at",
        )
    )
    return action
