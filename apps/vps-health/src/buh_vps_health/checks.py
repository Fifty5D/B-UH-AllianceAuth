from pathlib import Path

from django.conf import settings
from django.core.checks import Warning, register


@register()
def vps_health_checks(app_configs, **kwargs):
    del app_configs, kwargs
    warnings = []
    schedule = getattr(settings, "CELERYBEAT_SCHEDULE", {})
    if not any(
        isinstance(item, dict)
        and item.get("task") == "buh_vps_health.tasks.capture_health_snapshot"
        for item in schedule.values()
    ):
        warnings.append(
            Warning(
                "VPS Health snapshot schedule is not configured.",
                hint="Run the B-UH VPS Health updater again.",
                id="buh_vps_health.W001",
            )
        )
    socket_path = Path(
        getattr(
            settings,
            "BUH_VPS_HEALTH_AGENT_SOCKET",
            "/run/buh-vps-health/agent.sock",
        )
    )
    if not socket_path.exists():
        warnings.append(
            Warning(
                "The VPS Health host-agent socket is not currently mounted.",
                hint="The installer mounts it when live Auth containers are recreated.",
                id="buh_vps_health.W002",
            )
        )
    return warnings
