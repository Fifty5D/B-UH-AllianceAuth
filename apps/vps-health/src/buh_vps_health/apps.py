from django.apps import AppConfig


class BuhVpsHealthConfig(AppConfig):
    name = "buh_vps_health"
    label = "buh_vps_health"
    verbose_name = "B-UH VPS Health"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401
