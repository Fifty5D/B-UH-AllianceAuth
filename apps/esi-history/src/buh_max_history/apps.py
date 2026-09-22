"""Django application configuration."""

from django.apps import AppConfig


class BuhMaxHistoryConfig(AppConfig):
    """Configure B-UH maximum-history commands and checks."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "buh_max_history"
    verbose_name = "B-UH ESI History Archive"

    def ready(self) -> None:
        from . import checks  # noqa: F401
        from .capture import install_esi_archive_hooks
        from .memberaudit_compat import install_memberaudit_esi_status_guard

        install_esi_archive_hooks()
        install_memberaudit_esi_status_guard()
        from .local_history import install_business_history_hooks

        install_business_history_hooks()
