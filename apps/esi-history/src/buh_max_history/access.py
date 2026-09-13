from django.contrib.auth import get_user_model
from django.db.models import Q

PERMISSION_CODES = (
    "view_history_archive",
    "view_archive_storage",
    "view_archive_coverage",
    "view_public_archive",
    "view_private_archive_metadata",
    "download_public_archive",
    "download_private_archive",
    "run_public_archive_sync",
    "manage_archive_settings",
    "manage_archive_access",
)


def has_permission(user, codename):
    return bool(
        user.is_authenticated
        and (user.is_superuser or user.has_perm(f"buh_max_history.{codename}"))
    )


def has_app_access(user):
    return has_permission(user, "view_history_archive")


def permission_map(user):
    return {code: has_permission(user, code) for code in PERMISSION_CODES}


def archive_administrators():
    User = get_user_model()
    return (
        User.objects.filter(is_active=True)
        .filter(
            Q(is_superuser=True)
            | Q(
                user_permissions__content_type__app_label="buh_max_history",
                user_permissions__codename="manage_archive_settings",
            )
            | Q(
                groups__permissions__content_type__app_label="buh_max_history",
                groups__permissions__codename="manage_archive_settings",
            )
        )
        .distinct()
    )
