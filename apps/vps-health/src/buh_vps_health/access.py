from django.contrib.auth import get_user_model
from django.db.models import Q

PERMISSION_CODES = (
    "view_vps_health",
    "view_host_metrics",
    "view_container_metrics",
    "view_worker_workload",
    "view_task_queue",
    "view_action_history",
    "check_updates",
    "view_update_results",
    "restart_auth_services",
    "scale_workers",
    "manage_health_settings",
    "manage_health_access",
)


def has_permission(user, codename):
    return bool(
        user.is_authenticated
        and (user.is_superuser or user.has_perm(f"buh_vps_health.{codename}"))
    )


def has_app_access(user):
    return has_permission(user, "view_vps_health")


def permission_map(user):
    return {code: has_permission(user, code) for code in PERMISSION_CODES}


def notification_recipients():
    User = get_user_model()
    return (
        User.objects.filter(is_active=True)
        .filter(
            Q(is_superuser=True)
            | Q(
                user_permissions__content_type__app_label="buh_vps_health",
                user_permissions__codename__in=(
                    "manage_health_settings",
                    "restart_auth_services",
                ),
            )
            | Q(
                groups__permissions__content_type__app_label="buh_vps_health",
                groups__permissions__codename__in=(
                    "manage_health_settings",
                    "restart_auth_services",
                ),
            )
        )
        .distinct()
    )
