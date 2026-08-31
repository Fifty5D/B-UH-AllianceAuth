"""Deployment checks that catch incomplete installations before startup."""

from importlib.util import find_spec

from django.conf import settings
from django.core.checks import Error, Warning, register


@register()
def structure_ops_checks(app_configs, **kwargs):
    del app_configs, kwargs
    messages = []
    for module, package in (
        ("structures", "aa-structures"),
        ("moonmining", "aa-moonmining"),
    ):
        if find_spec(module) is None:
            messages.append(
                Error(
                    f"{package} is not installed.",
                    hint="Re-run the B-UH Structure Operations updater.",
                    id="buh_structure_ops.E001",
                )
            )
    schedule = getattr(settings, "CELERYBEAT_SCHEDULE", {})
    required_tasks = {
        "structures.tasks.update_all_structures",
        "structures.tasks.fetch_all_notifications",
        "moonmining.tasks.run_regular_updates",
        "buh_structure_ops.tasks.capture_and_evaluate",
    }
    configured = {
        entry.get("task") for entry in schedule.values() if isinstance(entry, dict)
    }
    missing = required_tasks - configured
    if missing:
        messages.append(
            Warning(
                "Structure Operations background schedules are incomplete.",
                hint=f"Re-run the updater. Missing tasks: {', '.join(sorted(missing))}",
                id="buh_structure_ops.W001",
            )
        )
    return messages
