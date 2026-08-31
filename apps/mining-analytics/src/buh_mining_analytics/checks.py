"""Django system checks for common installation mistakes."""

from importlib.metadata import PackageNotFoundError, version

from django.conf import settings
from django.core.checks import Error, Warning, register
from packaging.version import Version

from . import app_settings


@register()
def mining_analytics_checks(app_configs, **kwargs):
    """Validate the app, dependencies, and safety limits."""

    del app_configs, kwargs
    issues = []

    if "memberaudit" not in settings.INSTALLED_APPS:
        issues.append(
            Error(
                "B-UH Mining Analytics requires Member Audit in INSTALLED_APPS.",
                id="buh_mining_analytics.E001",
            )
        )

    for package, minimum, maximum in (
        ("allianceauth", Version("5.0"), Version("6.0")),
        ("aa-memberaudit", Version("5.0"), Version("6.0")),
    ):
        try:
            installed = Version(version(package))
        except PackageNotFoundError:
            issues.append(
                Error(
                    f"Required package {package} is not installed.",
                    id="buh_mining_analytics.E002",
                )
            )
        else:
            if installed < minimum or installed >= maximum:
                issues.append(
                    Error(
                        f"{package} {installed} is outside the supported range "
                        f">={minimum}, <{maximum}.",
                        id="buh_mining_analytics.E003",
                    )
                )

    if app_settings.MAX_SELECTED_CHARACTERS > 2_000:
        issues.append(
            Warning(
                "BUH_MINING_MAX_SELECTED_CHARACTERS is above 2,000 and may make "
                "corporation dashboard requests expensive.",
                id="buh_mining_analytics.W001",
            )
        )

    return issues
