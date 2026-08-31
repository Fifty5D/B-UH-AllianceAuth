"""Alliance Auth admin integration."""

from allianceauth.groupmanagement.models import ReservedGroupName
from django.contrib import admin, messages
from django.db.models import Count, Prefetch
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html

from .access_roles import replace_access_configuration
from .forms import (
    StructureOpsAccessGroupForm,
    moon_permissions,
    navigation_permissions,
    ops_permissions,
    schedule_permissions,
)
from .models import (
    AccessRoleConfiguration,
    AlertEvent,
    ScheduleEvent,
    StructureOpsAccessGroup,
    StructurePreference,
    StructureSnapshot,
    TrackedCorporation,
    WarState,
)


class FullAdminOnlyMixin:
    def _can_admin(self, request):
        return request.user.is_superuser or request.user.has_perm(
            "buh_structure_ops.admin_structure_ops"
        )

    def has_module_permission(self, request):
        return self._can_admin(request)

    def has_view_permission(self, request, obj=None):
        return self._can_admin(request)

    def has_change_permission(self, request, obj=None):
        return self._can_admin(request)

    def has_add_permission(self, request):
        return self._can_admin(request)

    def has_delete_permission(self, request, obj=None):
        return self._can_admin(request)


@admin.register(StructureOpsAccessGroup)
class StructureOpsAccessGroupAdmin(FullAdminOnlyMixin, admin.ModelAdmin):
    """Manage only this app's permissions on existing Auth groups."""

    form = StructureOpsAccessGroupForm
    ordering = ("name",)
    search_fields = ("name",)
    list_display = (
        "name",
        "member_count",
        "moon_mining_access",
        "moon_amount_access",
        "moon_action_access",
        "schedule_access",
        "administrator_access",
        "full_group_link",
    )
    fields = (
        "name",
        "adopt_reserved_role",
        "members",
        "ops_permissions",
        "navigation_permissions",
        "moon_permissions",
        "schedule_permissions",
        "permission_guide",
        "full_group_link",
    )
    readonly_fields = ("permission_guide", "full_group_link")
    save_on_top = True
    actions = ("remove_structure_ops_access",)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(ops_member_count=Count("user", distinct=True))
            .prefetch_related(
                Prefetch(
                    "permissions",
                    queryset=ops_permissions(),
                    to_attr="admin_ops_permissions",
                ),
                Prefetch(
                    "permissions",
                    queryset=navigation_permissions(),
                    to_attr="admin_navigation_permissions",
                ),
                Prefetch(
                    "permissions",
                    queryset=moon_permissions(),
                    to_attr="admin_moon_permissions",
                ),
                Prefetch(
                    "permissions",
                    queryset=schedule_permissions(),
                    to_attr="admin_schedule_permissions",
                ),
            )
        )

    def save_model(self, request, obj, form, change):
        if form.cleaned_data.get("adopt_reserved_role"):
            removed, _ = ReservedGroupName.objects.filter(
                name__iexact=form.cleaned_data["name"]
            ).delete()
            if removed:
                self.message_user(
                    request,
                    "The reserved-name entry was removed so Alliance Auth can manage "
                    f'the existing Discord role "{form.cleaned_data["name"]}".',
                )
        super().save_model(request, obj, form, change)
        obj.user_set.set(form.cleaned_data["members"])
        for field_name, available_queryset in (
            ("ops_permissions", ops_permissions()),
            ("navigation_permissions", navigation_permissions()),
            ("moon_permissions", moon_permissions()),
            ("schedule_permissions", schedule_permissions()),
        ):
            available = list(available_queryset)
            selected = {
                permission.pk for permission in form.cleaned_data[field_name]
            }
            obj.permissions.remove(
                *(item for item in available if item.pk not in selected)
            )
            obj.permissions.add(*(item for item in available if item.pk in selected))

    @admin.action(
        description=(
            "Remove Structure Operations access only "
            "(preserve Auth group and Discord role)"
        )
    )
    def remove_structure_ops_access(self, request, queryset):
        available = {
            permission.pk: permission
            for source in (
                ops_permissions(),
                navigation_permissions(),
                moon_permissions(),
                schedule_permissions(),
            )
            for permission in source
        }
        for group in queryset:
            group.permissions.remove(*available.values())
        self.message_user(
            request,
            f"Removed Structure Operations permissions from {queryset.count()} "
            "group(s). Auth groups, memberships, Discord roles, and unrelated "
            "permissions were preserved.",
            messages.SUCCESS,
        )

    @staticmethod
    def _codes(obj):
        prefetched = getattr(obj, "admin_ops_permissions", None)
        if prefetched is not None:
            return {permission.codename for permission in prefetched}
        return set(
            obj.permissions.filter(
                content_type__app_label="buh_structure_ops"
            ).values_list("codename", flat=True)
        )

    @staticmethod
    def _permission_codes(obj, app_label, attr_name):
        prefetched = getattr(obj, attr_name, None)
        if prefetched is not None:
            return {permission.codename for permission in prefetched}
        return set(
            obj.permissions.filter(content_type__app_label=app_label).values_list(
                "codename", flat=True
            )
        )

    @admin.display(description="Members", ordering="ops_member_count")
    def member_count(self, obj):
        return getattr(obj, "ops_member_count", obj.user_set.count())

    @admin.display(boolean=True, description="Viewer")
    def viewer_access(self, obj):
        return "view_structure_ops" in self._codes(obj)

    @admin.display(boolean=True, description="Manager")
    def manager_access(self, obj):
        return "manage_structure_ops" in self._codes(obj)

    @admin.display(boolean=True, description="Moon tab")
    def moon_mining_access(self, obj):
        codes = self._permission_codes(
            obj, "moonmining", "admin_navigation_permissions"
        )
        return "basic_access" in codes and "extractions_access" in codes

    @admin.display(boolean=True, description="Moon amounts")
    def moon_amount_access(self, obj):
        codes = self._permission_codes(obj, "moonmining", "admin_moon_permissions")
        return "view_extraction_amounts" in codes

    @admin.display(boolean=True, description="Moon buttons")
    def moon_action_access(self, obj):
        codes = self._permission_codes(obj, "moonmining", "admin_moon_permissions")
        return {
            "view_extraction_details",
            "view_moon_details",
        }.issubset(codes)

    @admin.display(boolean=True, description="Schedule")
    def schedule_access(self, obj):
        codes = self._permission_codes(
            obj, "buh_structure_ops", "admin_schedule_permissions"
        )
        return "view_schedule" in codes

    @admin.display(boolean=True, description="Full admin")
    def administrator_access(self, obj):
        return "admin_structure_ops" in self._codes(obj)

    @admin.display(description="Full group editor")
    def full_group_link(self, obj):
        if not obj or not obj.pk:
            return "Save the group before opening the full editor."
        try:
            url = reverse("admin:groupmanagement_group_change", args=(obj.pk,))
        except NoReverseMatch:
            return "Alliance Auth group editor unavailable"
        return format_html('<a href="{}">Open full Alliance Auth group</a>', url)

    @admin.display(description="Access level guide")
    def permission_guide(self, obj):
        del obj
        return format_html(
            "<ul>"
            "<li><strong>Viewer</strong> — See every tracked corporation, structure, fuel forecast, extraction, timer, rig, war, and alert.</li>"
            "<li><strong>Manager</strong> — Viewer access plus refresh, alert acknowledgement, notes, refill targets, and CSV export.</li>"
            "<li><strong>Full administrator</strong> — Manager access plus owner authorization setup and this access editor.</li>"
            "<li><strong>Application visibility</strong> — Controls sidebar entries and Moon Mining sub-tabs.</li>"
            "<li><strong>Moon data & actions</strong> — Amounts, hammer details, moon details, ledgers, scans, and owner setup are all separate.</li>"
            "<li><strong>Schedule</strong> — The tab, moon pops, fuel, structure timers, wars, custom events, and each custom-event action are separate.</li>"
            "</ul>"
        )


@admin.register(AccessRoleConfiguration)
class AccessRoleConfigurationAdmin(FullAdminOnlyMixin, admin.ModelAdmin):
    """Select which two existing groups receive the safe defaults."""

    fields = ("member_group", "director_group", "updated_at")
    readonly_fields = ("updated_at",)
    list_display = ("member_group", "director_group", "updated_at")

    def has_add_permission(self, request):
        return self._can_admin(request) and not AccessRoleConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        previous_member_id = None
        previous_director_id = None
        if change and obj.pk:
            previous = AccessRoleConfiguration.objects.filter(pk=obj.pk).first()
            if previous:
                previous_member_id = previous.member_group_id
                previous_director_id = previous.director_group_id
        super().save_model(request, obj, form, change)
        replace_access_configuration(
            obj,
            previous_member_id=previous_member_id,
            previous_director_id=previous_director_id,
        )
        self.message_user(
            request,
            "Member and Director defaults were applied to the selected existing groups.",
        )


@admin.register(ScheduleEvent)
class ScheduleEventAdmin(FullAdminOnlyMixin, admin.ModelAdmin):
    list_display = ("title", "starts_at", "ends_at", "all_day", "color", "updated_by")
    list_filter = ("all_day", "color")
    search_fields = ("title", "description", "location")
    readonly_fields = ("created_by", "updated_by", "created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(TrackedCorporation)
class TrackedCorporationAdmin(FullAdminOnlyMixin, admin.ModelAdmin):
    list_display = (
        "corporation_name",
        "corporation_id",
        "is_required",
        "is_enabled",
        "updated_at",
    )
    list_filter = ("is_required", "is_enabled")
    search_fields = ("corporation_name", "corporation_id")


@admin.register(StructurePreference)
class StructurePreferenceAdmin(FullAdminOnlyMixin, admin.ModelAdmin):
    list_display = ("structure_id", "target_fuel_days", "updated_by", "updated_at")
    search_fields = ("structure_id", "notes")


@admin.register(AlertEvent)
class AlertEventAdmin(FullAdminOnlyMixin, admin.ModelAdmin):
    list_display = (
        "title",
        "kind",
        "severity",
        "is_active",
        "occurred_at",
        "acknowledged_by",
    )
    list_filter = ("kind", "severity", "is_active")
    search_fields = ("title", "message", "event_key", "structure_id")
    readonly_fields = tuple(field.name for field in AlertEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(WarState)
class WarStateAdmin(FullAdminOnlyMixin, admin.ModelAdmin):
    list_display = (
        "corporation_id",
        "last_event_type",
        "is_active",
        "war_hq_name",
        "updated_at",
    )
    list_filter = ("is_active", "last_event_type")
    readonly_fields = tuple(field.name for field in WarState._meta.fields)


@admin.register(StructureSnapshot)
class StructureSnapshotAdmin(FullAdminOnlyMixin, admin.ModelAdmin):
    list_display = (
        "structure_name",
        "corporation_id",
        "captured_at",
        "fuel_quantity",
        "fuel_blocks_per_day",
    )
    list_filter = ("corporation_id",)
    search_fields = ("structure_name", "structure_id")
    readonly_fields = tuple(field.name for field in StructureSnapshot._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
