"""Django application configuration."""

from django.apps import AppConfig, apps


class BuhMemberauditAutoregConfig(AppConfig):
    """Configure automatic Member Audit registration."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "buh_memberaudit_autoreg"
    verbose_name = "B-UH Member Audit Auto-Registration"

    def ready(self) -> None:
        # Importing registers the Django system checks.
        from . import checks  # noqa: F401, PLC0415

        # Let the system check report a useful configuration error instead of
        # crashing startup if an admin forgot to enable Member Audit.
        if apps.is_installed("memberaudit"):
            from . import signals  # noqa: F401, PLC0415
