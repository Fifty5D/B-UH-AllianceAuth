"""Django application configuration."""

from django.apps import AppConfig


class BuhMoonTaxConfig(AppConfig):
    """Application configuration for B-UH Moon Tax."""

    name = "buh_moon_tax"
    label = "buh_moon_tax"
    verbose_name = "B-UH Moon Tax"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401
