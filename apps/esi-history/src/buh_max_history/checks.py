"""Django checks for maximum-history retention."""

from django.core.checks import Warning, register


@register()
def check_unlimited_memberaudit_retention(app_configs=None, **kwargs):
    """Warn when Member Audit can still delete locally stored history."""

    del app_configs, kwargs

    from memberaudit import app_settings

    if app_settings.MEMBERAUDIT_DATA_RETENTION_LIMIT is None:
        return []

    return [
        Warning(
            "Member Audit historical-data retention is not unlimited.",
            hint=(
                "Set MEMBERAUDIT_DATA_RETENTION_LIMIT = None in local.py to keep "
                "all mail, contract, and wallet history already imported from ESI."
            ),
            id="buh_max_history.W001",
        )
    ]
