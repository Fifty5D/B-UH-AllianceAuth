"""Central permission helpers used by menus, views, APIs, and exports."""

from django.core.exceptions import PermissionDenied

APP_LABEL = "buh_moon_tax"


def has(user, codename: str) -> bool:
    return bool(
        user
        and user.is_authenticated
        and (user.is_superuser or user.has_perm(f"{APP_LABEL}.{codename}"))
    )


def has_app_access(user) -> bool:
    return has(user, "view_moon_tax") or has(user, "view_own_tax") or has(
        user, "view_all_tax"
    )


def can_view_assessment(user, assessment) -> bool:
    if has(user, "view_all_tax"):
        return True
    return has(user, "view_own_tax") and assessment.auth_user_id == user.pk


def require(user, codename: str, message: str | None = None) -> None:
    if not has(user, codename):
        raise PermissionDenied(message or "You do not have permission for this action.")


def visibility(user) -> dict[str, bool]:
    """Return one stable permission matrix for templates and JSON serializers."""

    return {
        code: has(user, code)
        for code in (
            "view_moon_tax",
            "view_own_tax",
            "view_all_tax",
            "view_unlinked_miners",
            "view_mining_quantities",
            "view_mining_values",
            "view_payment_evidence",
            "run_tax_audit",
            "review_payments",
            "manage_bill_adjustments",
            "preview_member_view",
            "manage_tax_policy",
            "manage_exemptions",
            "manage_enforcement",
            "export_tax_data",
            "manage_access",
        )
    }
