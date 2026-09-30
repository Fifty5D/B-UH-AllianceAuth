"""Django system checks for safe configuration."""

from django.apps import apps
from django.conf import settings
from django.core.checks import Error, register


@register()
def check_configuration(app_configs, **kwargs):  # noqa: ARG001
    """Ensure Auth requests every scope required by the installed Member Audit."""

    errors = []
    if not apps.is_installed("memberaudit"):
        return [
            Error(
                "B-UH Member Audit Auto-Registration requires 'memberaudit' in INSTALLED_APPS.",
                id="buh_memberaudit_autoreg.E001",
            )
        ]

    # This import is intentionally delayed until Django's app registry is ready.
    from memberaudit.models import Character  # noqa: PLC0415

    configured = set(getattr(settings, "LOGIN_TOKEN_SCOPES", ()))
    required = set(Character.esi_scopes()) | {"publicData"}
    missing = sorted(required - configured)
    if missing:
        errors.append(
            Error(
                "LOGIN_TOKEN_SCOPES is missing scopes required for automatic Member Audit registration.",
                hint="Add these scopes: " + ", ".join(missing),
                id="buh_memberaudit_autoreg.E002",
            )
        )
    return errors
