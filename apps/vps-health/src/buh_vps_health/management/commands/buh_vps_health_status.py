import json
from importlib.metadata import PackageNotFoundError, version

from django.core.management.base import BaseCommand

from buh_vps_health.agent_client import AgentUnavailable, get_metrics
from buh_vps_health.models import (
    AlertState,
    HealthConfiguration,
    MetricSnapshot,
    OperationalAction,
)


class Command(BaseCommand):
    help = "Show VPS Health configuration, host-agent, and collection status."

    def handle(self, *args, **options):
        del args, options
        try:
            package_version = version("aa-buh-vps-health")
        except PackageNotFoundError:
            package_version = "source checkout"
        config = HealthConfiguration.get_solo()
        latest = MetricSnapshot.objects.order_by("-captured_at").first()
        self.stdout.write(f"B-UH VPS Health {package_version}")
        self.stdout.write(
            f"Live refresh: {config.live_refresh_seconds}s | snapshot: "
            f"{config.snapshot_interval_minutes}m | retention: "
            f"{config.history_retention_days}d"
        )
        self.stdout.write(
            f"Worker safety range: {config.worker_minimum}-{config.worker_maximum}"
        )
        if latest:
            self.stdout.write(
                f"Latest snapshot: {latest.captured_at.isoformat()} | "
                f"CPU {latest.cpu_percent:.1f}% | RAM {latest.memory_percent:.1f}% | "
                f"disk {latest.disk_percent:.1f}% | workers "
                f"{latest.worker_online}/{latest.worker_desired} | queue "
                f"{latest.queued_tasks}"
            )
        else:
            self.stdout.write(
                self.style.WARNING("No historical snapshot has been stored yet.")
            )
        self.stdout.write(
            f"Active alerts: {AlertState.objects.filter(active=True).count()} | "
            f"unfinished actions: {OperationalAction.objects.exclude(status__in=['SUCCEEDED', 'FAILED']).count()}"
        )
        try:
            metrics = get_metrics()
        except AgentUnavailable as exc:
            self.stdout.write(self.style.ERROR(f"Host agent: unavailable ({exc})"))
            return
        self.stdout.write(self.style.SUCCESS("Host agent: responding"))
        self.stdout.write(json.dumps(metrics, indent=2, sort_keys=True))
