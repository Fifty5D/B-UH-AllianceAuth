"""Django application configuration."""

from django.apps import AppConfig


class BuhStructureOpsConfig(AppConfig):
    """Application configuration for B-UH Structure Operations."""

    name = "buh_structure_ops"
    label = "buh_structure_ops"
    verbose_name = "B-UH Structure Operations"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401
        from .discord_owner import install_discord_owner_nickname_guard
        from .moonmining_compat import install_moonmining_status_guard
        from .token_refresh import install_token_refresh_guard

        install_discord_owner_nickname_guard()
        install_token_refresh_guard()
        install_moonmining_status_guard()
