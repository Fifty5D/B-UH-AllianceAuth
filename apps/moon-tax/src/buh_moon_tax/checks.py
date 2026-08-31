"""Deployment checks that fail safely before a bad container reaches production."""

from django.conf import settings
from django.core.checks import Error, Warning, register
from django.db.utils import OperationalError, ProgrammingError

from .models import TaxConfiguration

REQUIRED_APPS = ("memberaudit", "moonmining", "buh_structure_ops")
REQUIRED_CHARACTER_SCOPES = {
    "esi-wallet.read_character_wallet.v1",
    "esi-contracts.read_character_contracts.v1",
}


@register()
def moon_tax_checks(app_configs, **kwargs):
    del app_configs, kwargs
    issues = []
    installed = set(settings.INSTALLED_APPS)
    for app in REQUIRED_APPS:
        if app not in installed:
            issues.append(
                Error(
                    f"B-UH Moon Tax requires {app} in INSTALLED_APPS.",
                    id="buh_moon_tax.E001",
                )
            )

    configured_scopes = set(getattr(settings, "LOGIN_TOKEN_SCOPES", ()))
    missing = REQUIRED_CHARACTER_SCOPES - configured_scopes
    if missing:
        issues.append(
            Error(
                "LOGIN_TOKEN_SCOPES is missing Moon Tax payment-audit scopes: "
                + ", ".join(sorted(missing)),
                hint="Run the supplied updater; existing limited tokens must be reauthorized.",
                id="buh_moon_tax.E002",
            )
        )

    schedule = getattr(settings, "CELERYBEAT_SCHEDULE", {})
    if not any(
        item.get("task") == "buh_moon_tax.tasks.run_scheduled_audit"
        for item in schedule.values()
    ):
        issues.append(
            Warning(
                "No automatic Moon Tax audit is present in CELERYBEAT_SCHEDULE.",
                hint="Use the supplied updater or add buh_moon_tax.tasks.run_scheduled_audit.",
                id="buh_moon_tax.W001",
            )
        )

    try:
        config = TaxConfiguration.objects.filter(singleton_id=1).first()
    except (OperationalError, ProgrammingError):
        config = None
    if config and not (
        config.default_payment_recipients.filter(enabled=True).exists()
        or config.payment_character_id
        or config.payment_corporation_id
    ):
        issues.append(
            Warning(
                "Moon Tax has no enabled default payment destination.",
                hint=(
                    "Run auth buh_moon_tax_setup or select at least one default "
                    "recipient. Per-Athanor checklists may override the default."
                ),
                id="buh_moon_tax.W002",
            )
        )
    return issues
