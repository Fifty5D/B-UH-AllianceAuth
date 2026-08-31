"""Django app configuration."""

from django.apps import AppConfig


class BuhMiningAnalyticsConfig(AppConfig):
    """Application configuration for B-UH Mining Analytics."""

    name = "buh_mining_analytics"
    label = "buh_mining_analytics"
    verbose_name = "B-UH Mining Analytics"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Importing registers Django system checks.
        from . import checks  # noqa: F401
