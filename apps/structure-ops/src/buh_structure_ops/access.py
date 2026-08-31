"""Access helpers for views and notification recipients."""

from django.contrib.auth import get_user_model
from django.db.models import Q


def has_view_access(user):
    return bool(
        user.is_authenticated
        and (
            user.is_superuser
            or user.has_perm("buh_structure_ops.view_structure_ops")
            or user.has_perm("buh_structure_ops.manage_structure_ops")
            or user.has_perm("buh_structure_ops.admin_structure_ops")
        )
    )


def has_manager_access(user):
    return bool(
        user.is_authenticated
        and (
            user.is_superuser
            or user.has_perm("buh_structure_ops.manage_structure_ops")
            or user.has_perm("buh_structure_ops.admin_structure_ops")
        )
    )


def has_admin_access(user):
    return bool(
        user.is_authenticated
        and (
            user.is_superuser or user.has_perm("buh_structure_ops.admin_structure_ops")
        )
    )


def has_schedule_access(user):
    return bool(
        user.is_authenticated
        and (
            user.is_superuser
            or user.has_perm("buh_structure_ops.view_schedule")
            or user.has_perm("buh_structure_ops.admin_structure_ops")
        )
    )


def has_any_access(user):
    return has_view_access(user) or has_schedule_access(user)


def notification_recipients():
    """Active managers/admins, resolved without backend-specific with_perm behavior."""

    User = get_user_model()
    return (
        User.objects.filter(is_active=True)
        .filter(
            Q(is_superuser=True)
            | Q(
                user_permissions__content_type__app_label="buh_structure_ops",
                user_permissions__codename__in=(
                    "manage_structure_ops",
                    "admin_structure_ops",
                ),
            )
            | Q(
                groups__permissions__content_type__app_label="buh_structure_ops",
                groups__permissions__codename__in=(
                    "manage_structure_ops",
                    "admin_structure_ops",
                ),
            )
        )
        .distinct()
    )
