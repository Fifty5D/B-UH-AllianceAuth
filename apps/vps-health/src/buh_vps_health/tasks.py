from allianceauth.services.tasks import QueueOnce
from celery import shared_task
from django.utils.timezone import now

from .models import HealthConfiguration, MetricSnapshot
from .services import capture_and_evaluate


@shared_task(base=QueueOnce, once={"graceful": True})
def capture_health_snapshot():
    config = HealthConfiguration.get_solo()
    latest = MetricSnapshot.objects.order_by("-captured_at").first()
    if latest:
        age_seconds = (now() - latest.captured_at).total_seconds()
        if age_seconds < config.snapshot_interval_minutes * 60 - 5:
            return {"ok": True, "skipped": True, "reason": "Snapshot interval not due."}
    return capture_and_evaluate()
