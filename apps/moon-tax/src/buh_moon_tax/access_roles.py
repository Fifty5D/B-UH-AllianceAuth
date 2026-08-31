"""Apply editable Member/Director presets without hard-coding Discord role names."""

from django.contrib.auth.models import Group, Permission
from django.db import transaction

MEMBER_CODES = (
    "view_moon_tax",
    "view_own_tax",
    "view_mining_quantities",
    "view_mining_values",
    "view_payment_evidence",
)

DIRECTOR_CODES = (
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


def selected_groups() -> tuple[Group, Group]:
    """Use Structure Operations as the shared source of role names when available."""

    try:
        from buh_structure_ops.models import AccessRoleConfiguration
    except ImportError:
        AccessRoleConfiguration = None
    if AccessRoleConfiguration is not None:
        config = (
            AccessRoleConfiguration.objects.select_related(
                "member_group", "director_group"
            )
            .filter(singleton_id=1)
            .first()
        )
        if config and config.member_group_id and config.director_group_id:
            return config.member_group, config.director_group

    member, _ = Group.objects.get_or_create(name="Member")
    director, _ = Group.objects.get_or_create(name="Director")
    return member, director


def permission_map() -> dict[str, Permission]:
    return {
        item.codename: item
        for item in Permission.objects.filter(content_type__app_label="buh_moon_tax")
    }


@transaction.atomic
def apply_role_presets(*, force: bool = False) -> tuple[Group, Group]:
    """Add first-install defaults without undoing a customized permission matrix."""

    member, director = selected_groups()
    available = permission_map()
    missing = (set(MEMBER_CODES) | set(DIRECTOR_CODES)) - set(available)
    if missing:
        raise Permission.DoesNotExist(
            f"Moon Tax permissions are missing: {', '.join(sorted(missing))}"
        )
    already_configured = Group.objects.filter(
        permissions__content_type__app_label="buh_moon_tax"
    ).exists()
    if force or not already_configured:
        member.permissions.add(*(available[code] for code in MEMBER_CODES))
        director.permissions.add(*(available[code] for code in DIRECTOR_CODES))
    return member, director


def director_users():
    """Return active users selected by the editable shared Director role."""

    _, director = selected_groups()
    return director.user_set.filter(is_active=True).distinct()
