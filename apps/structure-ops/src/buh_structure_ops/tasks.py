"""Celery entry points for balanced background synchronization."""

from allianceauth.services.tasks import QueueOnce
from celery import shared_task

from .services import capture_snapshots, evaluate_all, synchronize_tracked_corporations


@shared_task(base=QueueOnce, once={"graceful": True})
def capture_and_evaluate():
    """Capture current values, evaluate events, and notify managers/admins."""

    synchronize_tracked_corporations()
    snapshots = capture_snapshots()
    results = evaluate_all()
    results["snapshots"] = snapshots
    return results


@shared_task(base=QueueOnce, once={"graceful": True})
def queue_source_refreshes():
    """Queue each upstream refresh without bypassing its rate-limit safeguards."""

    from moonmining.tasks import run_regular_updates
    from structures.tasks import fetch_all_notifications, update_all_structures

    update_all_structures.delay()
    fetch_all_notifications.delay()
    run_regular_updates.delay()
    capture_and_evaluate.apply_async(countdown=90)
    return True
