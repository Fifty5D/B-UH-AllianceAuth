from allianceauth.authentication.core.celery_workers import queued_tasks_count
from celery import current_app
from django.core.cache import cache

CACHE_KEY = "buh-vps-health-worker-workload-v1"


def _task_label(task):
    return task.get("name") or task.get("type") or "Unknown task"


def get_worker_workload(force=False):
    if not force:
        cached = cache.get(CACHE_KEY)
        if cached:
            return cached

    result = {
        "available": False,
        "queued": None,
        "active_total": 0,
        "reserved_total": 0,
        "scheduled_total": 0,
        "nodes": [],
        "error": "",
    }
    try:
        inspector = current_app.control.inspect(timeout=0.8)
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        scheduled = inspector.scheduled() or {}
        stats = inspector.stats() or {}
        names = sorted(set(active) | set(reserved) | set(scheduled) | set(stats))
        for name in names:
            active_tasks = active.get(name) or []
            reserved_tasks = reserved.get(name) or []
            scheduled_tasks = scheduled.get(name) or []
            node_stats = stats.get(name) or {}
            pool = node_stats.get("pool") or {}
            result["nodes"].append(
                {
                    "name": name,
                    "active": len(active_tasks),
                    "reserved": len(reserved_tasks),
                    "scheduled": len(scheduled_tasks),
                    "concurrency": pool.get("max-concurrency")
                    or pool.get("max_concurrency"),
                    "tasks": [_task_label(task) for task in active_tasks[:12]],
                }
            )
        result["queued"] = queued_tasks_count()
        result["active_total"] = sum(item["active"] for item in result["nodes"])
        result["reserved_total"] = sum(item["reserved"] for item in result["nodes"])
        result["scheduled_total"] = sum(item["scheduled"] for item in result["nodes"])
        result["available"] = bool(names)
        if not names:
            result["error"] = "No Celery workers answered inspection."
    except Exception as exc:  # noqa: BLE001 - transports expose many exception types.
        result["error"] = f"Celery inspection failed: {exc}"
    cache.set(CACHE_KEY, result, 10)
    return result
