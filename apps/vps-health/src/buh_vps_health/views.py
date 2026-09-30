from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import render
from django.utils.timezone import now
from django.views.decorators.http import require_GET, require_POST

from . import __version__
from .access import has_app_access, has_permission, permission_map
from .agent_client import AgentUnavailable, get_metrics, submit_action
from .models import AlertState, HealthConfiguration, MetricSnapshot, OperationalAction
from .services import synchronize_action
from .workload import get_worker_workload


def _require(user, codename):
    if not has_permission(user, codename):
        raise PermissionDenied


def _serialize_action(action):
    return {
        "id": str(action.pk),
        "action": action.action,
        "label": action.get_action_display(),
        "target_worker_count": action.target_worker_count,
        "requested_by": action.requested_by.username,
        "status": action.status,
        "status_label": action.get_status_display(),
        "message": action.message,
        "result": action.result_payload,
        "requested_at": action.requested_at.isoformat(),
        "started_at": action.started_at.isoformat() if action.started_at else None,
        "finished_at": action.finished_at.isoformat() if action.finished_at else None,
    }


def _serialize_alert(alert):
    return {
        "key": alert.key,
        "title": alert.title,
        "message": alert.message,
        "severity": alert.severity,
        "active": alert.active,
        "last_seen_at": alert.last_seen_at.isoformat(),
    }


@login_required
def dashboard(request):
    if not has_app_access(request.user):
        raise PermissionDenied
    config = HealthConfiguration.get_solo()
    permissions = permission_map(request.user)
    return render(
        request,
        "buh_vps_health/dashboard.html",
        {
            "app_name": "VPS Health",
            "app_version": __version__,
            "config": config,
            "permissions": permissions,
            "alerts": AlertState.objects.filter(active=True)[:8],
            "actions": OperationalAction.objects.select_related("requested_by")[:12]
            if permissions["view_action_history"]
            else (),
        },
    )


@require_GET
@login_required
def live_metrics(request):
    if not has_app_access(request.user):
        raise PermissionDenied
    permissions = permission_map(request.user)
    try:
        metrics = get_metrics()
        agent_error = ""
    except AgentUnavailable as exc:
        metrics = {}
        agent_error = str(exc)

    raw_workload = (
        get_worker_workload()
        if permissions["view_worker_workload"] or permissions["view_task_queue"]
        else {}
    )
    workload = {}
    if permissions["view_worker_workload"]:
        workload.update(
            {
                key: raw_workload.get(key)
                for key in (
                    "available",
                    "active_total",
                    "reserved_total",
                    "scheduled_total",
                    "nodes",
                    "error",
                )
            }
        )
    if permissions["view_task_queue"]:
        workload["queued"] = raw_workload.get("queued")
    actions = []
    if permissions["view_action_history"]:
        queryset = OperationalAction.objects.select_related("requested_by")[:12]
        for action in queryset:
            try:
                action = synchronize_action(action)
            except AgentUnavailable:
                pass
            serialized = _serialize_action(action)
            if not permissions["view_update_results"]:
                serialized.pop("result", None)
            actions.append(serialized)

    latest_update = None
    if permissions["view_update_results"]:
        update_action = (
            OperationalAction.objects.filter(action=OperationalAction.Action.CHECK_UPDATES)
            .select_related("requested_by")
            .first()
        )
        if update_action:
            try:
                update_action = synchronize_action(update_action)
            except AgentUnavailable:
                pass
            latest_update = _serialize_action(update_action)

    payload = {
        "ok": not agent_error,
        "generated_at": now().isoformat(),
        "agent_error": agent_error,
        "host": metrics.get("host", {}) if permissions["view_host_metrics"] else {},
        "docker": metrics.get("docker", {})
        if permissions["view_container_metrics"]
        else {},
        "workers": metrics.get("workers", {})
        if permissions["view_worker_workload"] or permissions["scale_workers"]
        else {},
        "workload": workload,
        "alerts": [
            _serialize_alert(item)
            for item in AlertState.objects.filter(active=True)[:8]
        ],
        "actions": actions,
        "latest_update": latest_update,
        "permissions": permissions,
    }
    return JsonResponse(payload, status=200 if not agent_error else 503)


@require_GET
@login_required
def history(request):
    _require(request.user, "view_host_metrics")
    try:
        hours = min(24 * 30, max(1, int(request.GET.get("hours", "24"))))
    except ValueError:
        hours = 24
    from datetime import timedelta

    rows = list(
        MetricSnapshot.objects.filter(captured_at__gte=now() - timedelta(hours=hours))
        .order_by("captured_at")
        .values(
            "captured_at",
            "cpu_percent",
            "load_5",
            "memory_percent",
            "disk_percent",
            "network_rx_bytes_per_second",
            "network_tx_bytes_per_second",
            "active_tasks",
            "queued_tasks",
            "worker_online",
        )
    )
    if len(rows) > 600:
        stride = max(1, len(rows) // 600)
        rows = rows[::stride]
    return JsonResponse(
        {
            "hours": hours,
            "points": [
                {**row, "captured_at": row["captured_at"].isoformat()} for row in rows
            ],
        }
    )


@require_POST
@login_required
def restart_auth(request):
    _require(request.user, "restart_auth_services")
    if request.POST.get("confirmation") != "RESTART AUTH":
        return JsonResponse(
            {"ok": False, "error": "Confirmation did not match."}, status=400
        )
    action = OperationalAction.objects.create(
        action=OperationalAction.Action.RESTART_AUTH,
        requested_by=request.user,
        message="Waiting for the host agent.",
    )
    try:
        response = submit_action(
            action.pk,
            "restart_auth",
            request.user.username,
        )
    except AgentUnavailable as exc:
        action.status = OperationalAction.Status.FAILED
        action.message = str(exc)
        action.finished_at = now()
        action.save(update_fields=("status", "message", "finished_at"))
        return JsonResponse({"ok": False, "error": str(exc)}, status=503)
    action.agent_action_id = response["action"]["id"]
    action.status = OperationalAction.Status.ACCEPTED
    action.message = response["action"].get("message", "Restart accepted.")
    action.save(update_fields=("agent_action_id", "status", "message"))
    return JsonResponse({"ok": True, "action": _serialize_action(action)}, status=202)


@require_POST
@login_required
def set_workers(request):
    _require(request.user, "scale_workers")
    config = HealthConfiguration.get_solo()
    try:
        count = int(request.POST.get("count", ""))
    except ValueError:
        return JsonResponse(
            {"ok": False, "error": "Worker count must be a number."}, status=400
        )
    if not config.worker_minimum <= count <= config.worker_maximum:
        return JsonResponse(
            {
                "ok": False,
                "error": f"Worker count must be between {config.worker_minimum} and {config.worker_maximum}.",
            },
            status=400,
        )
    action = OperationalAction.objects.create(
        action=OperationalAction.Action.SET_WORKERS,
        target_worker_count=count,
        requested_by=request.user,
        message="Waiting for the host agent.",
    )
    try:
        response = submit_action(
            action.pk,
            "set_workers",
            request.user.username,
            worker_count=count,
        )
    except AgentUnavailable as exc:
        action.status = OperationalAction.Status.FAILED
        action.message = str(exc)
        action.finished_at = now()
        action.save(update_fields=("status", "message", "finished_at"))
        return JsonResponse({"ok": False, "error": str(exc)}, status=503)
    action.agent_action_id = response["action"]["id"]
    action.status = OperationalAction.Status.ACCEPTED
    action.message = response["action"].get("message", "Scaling accepted.")
    action.save(update_fields=("agent_action_id", "status", "message"))
    return JsonResponse({"ok": True, "action": _serialize_action(action)}, status=202)


@require_POST
@login_required
def check_updates(request):
    _require(request.user, "check_updates")
    if OperationalAction.objects.filter(
        action=OperationalAction.Action.CHECK_UPDATES,
        status__in=(
            OperationalAction.Status.QUEUED,
            OperationalAction.Status.ACCEPTED,
            OperationalAction.Status.RUNNING,
        ),
    ).exists():
        return JsonResponse(
            {"ok": False, "error": "An update scan is already running."}, status=409
        )
    action = OperationalAction.objects.create(
        action=OperationalAction.Action.CHECK_UPDATES,
        requested_by=request.user,
        message="Waiting for the host agent.",
    )
    try:
        response = submit_action(action.pk, "check_updates", request.user.username)
    except AgentUnavailable as exc:
        action.status = OperationalAction.Status.FAILED
        action.message = str(exc)
        action.finished_at = now()
        action.save(update_fields=("status", "message", "finished_at"))
        return JsonResponse({"ok": False, "error": str(exc)}, status=503)
    action.agent_action_id = response["action"]["id"]
    action.status = OperationalAction.Status.ACCEPTED
    action.message = response["action"].get("message", "Update scan accepted.")
    action.save(update_fields=("agent_action_id", "status", "message"))
    return JsonResponse({"ok": True, "action": _serialize_action(action)}, status=202)


@require_GET
@login_required
def action_detail(request, action_id):
    _require(request.user, "view_action_history")
    try:
        action = OperationalAction.objects.select_related("requested_by").get(
            pk=action_id
        )
    except OperationalAction.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Action not found."}, status=404)
    try:
        action = synchronize_action(action)
    except AgentUnavailable:
        pass
    payload = _serialize_action(action)
    if not has_permission(request.user, "view_update_results"):
        payload.pop("result", None)
    return JsonResponse({"ok": True, "action": payload})
