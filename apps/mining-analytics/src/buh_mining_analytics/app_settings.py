"""User-configurable settings with safe defaults."""

from django.conf import settings

APP_NAME = getattr(settings, "BUH_MINING_APP_NAME", "Mining Analytics")
DEFAULT_DAYS = max(1, int(getattr(settings, "BUH_MINING_DEFAULT_DAYS", 30)))
MAX_SELECTED_CHARACTERS = max(
    1, int(getattr(settings, "BUH_MINING_MAX_SELECTED_CHARACTERS", 500))
)
MAX_EXPORT_ROWS = max(
    100, int(getattr(settings, "BUH_MINING_MAX_EXPORT_ROWS", 100_000))
)
ENABLE_REFRESH = bool(getattr(settings, "BUH_MINING_ENABLE_REFRESH", True))
