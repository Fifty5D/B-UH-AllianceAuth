"""Runtime settings with safe defaults."""

from django.conf import settings

APP_NAME = getattr(settings, "BUH_MOON_TAX_APP_NAME", "Moon Tax")
AUDIT_INTERVAL_HOURS = int(getattr(settings, "BUH_MOON_TAX_AUDIT_HOURS", 4))
SOURCE_SETTLE_SECONDS = int(
    getattr(settings, "BUH_MOON_TAX_SOURCE_SETTLE_SECONDS", 180)
)
JITA_REGION_ID = int(getattr(settings, "BUH_MOON_TAX_JITA_REGION_ID", 10000002))
JITA_STATION_ID = int(
    getattr(settings, "BUH_MOON_TAX_JITA_STATION_ID", 60003760)
)
ESI_BASE_URL = getattr(
    settings, "BUH_MOON_TAX_ESI_BASE_URL", "https://esi.evetech.net/latest"
).rstrip("/")
ESI_TIMEOUT_SECONDS = int(getattr(settings, "BUH_MOON_TAX_ESI_TIMEOUT", 20))
PRICE_CACHE_HOURS = int(getattr(settings, "BUH_MOON_TAX_PRICE_CACHE_HOURS", 24))
MAX_REFRESH_CHARACTERS = int(
    getattr(settings, "BUH_MOON_TAX_MAX_REFRESH_CHARACTERS", 5000)
)
STALE_AUDIT_HOURS = int(getattr(settings, "BUH_MOON_TAX_STALE_AUDIT_HOURS", 2))
