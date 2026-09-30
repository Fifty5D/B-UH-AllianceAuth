from allianceauth.groupmanagement.models import ReservedGroupName
from django.contrib import admin, messages
from django.db.models import Count, Prefetch

from .forms import (
    HealthConfigurationForm,
    VpsHealthAccessGroupForm,
    health_permissions,
)
from .models import (
    AlertState,
    HealthConfiguration,
    MetricSnapshot,
    OperationalAction,
    VpsHealthAccessGroup,
)


class PermissionAdminMixin:
    permission = "manage_health_settings"

    def _allowed(self, request):
        return request.user.is_superuser or request.user.has_perm(
            f"buh_vps_health.{self.permission}"
        )

    def has_module_permission(self, request):
        return self._allowed(request)

    def has_view_permission(self, request, obj=None):
        return self._allowed(request)

    def has_add_permission(self, request):
        return self._allowed(request)

    def has_change_permission(self, request, obj=None):
        return self._allowed(request)

    def has_delete_permission(self, request, obj=None):
        return self._allowed(request)


@admin.register(VpsHealthAccessGroup)
class VpsHealthAccessGroupAdmin(PermissionAdminMixin, admin.ModelAdmin):
    permission = "manage_health_access"
    form = VpsHealthAccessGroupForm
    ordering = ("name",)
    search_fields = ("name",)
    list_display = (
        "name",
        "member_count",
        "host_view",
        "container_view",
        "worker_view",
        "update_control",
        "restart_control",
        "scale_control",
        "settings_control",
    )
    fields = ("name", "adopt_reserved_role", "members", "health_permissions")
    save_on_top = True
    actions = ("remove_vps_health_access",)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(health_member_count=Count("user", distinct=True))
            .prefetch_related(
                Prefetch(
                    "permissions",
                    queryset=health_permissions(),
                    to_attr="admin_health_permissions",
                )
            )
        )

    def save_model(self, request, obj, form, change):
        if form.cleaned_data.get("adopt_reserved_role"):
            ReservedGroupName.objects.filter(
                name__iexact=form.cleaned_data["name"]
            ).delete()
        super().save_model(request, obj, form, change)
        obj.user_set.set(form.cleaned_data["members"])
        available = list(health_permissions())
        selected = {item.pk for item in form.cleaned_data["health_permissions"]}
        obj.permissions.remove(*(item for item in available if item.pk not in selected))
        obj.permissions.add(*(item for item in available if item.pk in selected))

    @admin.action(
        description="Remove VPS Health access only; preserve the Auth/Discord group"
    )
    def remove_vps_health_access(self, request, queryset):
        permissions = list(health_permissions())
        for group in queryset:
            group.permissions.remove(*permissions)
        self.message_user(
            request,
            f"Removed VPS Health permissions from {queryset.count()} group(s).",
            messages.SUCCESS,
        )

    @staticmethod
    def _codes(obj):
        return {
            item.codename
            for item in getattr(obj, "admin_health_permissions", obj.permissions.all())
        }

    @admin.display(description="Members", ordering="health_member_count")
    def member_count(self, obj):
        return getattr(obj, "health_member_count", obj.user_set.count())

    @admin.display(boolean=True, description="Host")
    def host_view(self, obj):
        return "view_host_metrics" in self._codes(obj)

    @admin.display(boolean=True, description="Containers")
    def container_view(self, obj):
        return "view_container_metrics" in self._codes(obj)

    @admin.display(boolean=True, description="Workers")
    def worker_view(self, obj):
        return "view_worker_workload" in self._codes(obj)

    @admin.display(boolean=True, description="Restart")
    def restart_control(self, obj):
        return "restart_auth_services" in self._codes(obj)

    @admin.display(boolean=True, description="Updates")
    def update_control(self, obj):
        codes = self._codes(obj)
        return "check_updates" in codes and "view_update_results" in codes

    @admin.display(boolean=True, description="Scale")
    def scale_control(self, obj):
        return "scale_workers" in self._codes(obj)

    @admin.display(boolean=True, description="Settings")
    def settings_control(self, obj):
        return "manage_health_settings" in self._codes(obj)


@admin.register(HealthConfiguration)
class HealthConfigurationAdmin(PermissionAdminMixin, admin.ModelAdmin):
    form = HealthConfigurationForm
    fieldsets = (
        ("Live dashboard", {"fields": ("live_refresh_seconds",)}),
        (
            "History",
            {"fields": ("snapshot_interval_minutes", "history_retention_days")},
        ),
        (
            "Warning thresholds",
            {
                "fields": (
                    "warning_cpu_percent",
                    "warning_memory_percent",
                    "warning_disk_percent",
                    "warning_load_per_cpu",
                    "warning_queue_depth",
                    "alert_sustain_minutes",
                    "notify_directors",
                )
            },
        ),
        (
            "Worker scaling safety",
            {
                "fields": ("worker_minimum", "worker_maximum"),
                "description": "The hard upper safety limit is eight workers.",
            },
        ),
        ("Change record", {"fields": ("updated_by", "updated_at")}),
    )
    readonly_fields = ("updated_by", "updated_at")

    def has_add_permission(self, request):
        return self._allowed(request) and not HealthConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(OperationalAction)
class OperationalActionAdmin(PermissionAdminMixin, admin.ModelAdmin):
    permission = "view_action_history"
    list_display = (
        "requested_at",
        "action",
        "target_worker_count",
        "requested_by",
        "status",
        "finished_at",
    )
    list_filter = ("action", "status")
    search_fields = ("requested_by__username", "message", "agent_action_id")
    readonly_fields = tuple(field.name for field in OperationalAction._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return self._allowed(request) and obj is None

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MetricSnapshot)
class MetricSnapshotAdmin(PermissionAdminMixin, admin.ModelAdmin):
    list_display = (
        "captured_at",
        "cpu_percent",
        "memory_percent",
        "disk_percent",
        "worker_online",
        "active_tasks",
        "queued_tasks",
    )
    readonly_fields = tuple(field.name for field in MetricSnapshot._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return self._allowed(request) and obj is None

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AlertState)
class AlertStateAdmin(PermissionAdminMixin, admin.ModelAdmin):
    list_display = ("title", "severity", "active", "last_seen_at", "resolved_at")
    list_filter = ("active", "severity")
    search_fields = ("key", "title", "message")
    readonly_fields = tuple(field.name for field in AlertState._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return self._allowed(request) and obj is None

    def has_delete_permission(self, request, obj=None):
        return False
