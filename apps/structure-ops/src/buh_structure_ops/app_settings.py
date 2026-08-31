"""Typed access to deploy-time settings."""

from django.conf import settings

APP_NAME = getattr(settings, "BUH_STRUCTURE_OPS_APP_NAME", "B-UH Structure Operations")
FUEL_CRITICAL_DAYS = int(getattr(settings, "BUH_STRUCTURE_OPS_FUEL_CRITICAL_DAYS", 7))
FUEL_WARNING_DAYS = int(getattr(settings, "BUH_STRUCTURE_OPS_FUEL_WARNING_DAYS", 14))
FUEL_TARGET_DAYS = int(getattr(settings, "BUH_STRUCTURE_OPS_FUEL_TARGET_DAYS", 30))
EXTRACTION_REMINDER_HOURS = tuple(
    int(value)
    for value in getattr(
        settings,
        "BUH_STRUCTURE_OPS_EXTRACTION_REMINDER_HOURS",
        (168, 72, 24),
    )
)
STRUCTURE_STALE_MINUTES = int(
    getattr(settings, "BUH_STRUCTURE_OPS_STRUCTURE_STALE_MINUTES", 90)
)
EXTRACTION_STALE_MINUTES = int(
    getattr(settings, "BUH_STRUCTURE_OPS_EXTRACTION_STALE_MINUTES", 30)
)
EVENT_LOOKBACK_DAYS = int(
    getattr(settings, "BUH_STRUCTURE_OPS_EVENT_LOOKBACK_DAYS", 45)
)
SNAPSHOT_RETENTION_DAYS = int(
    getattr(settings, "BUH_STRUCTURE_OPS_SNAPSHOT_RETENTION_DAYS", 180)
)
