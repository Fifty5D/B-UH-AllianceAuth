"""Django admin integration for Mining Analytics access."""

from allianceauth.groupmanagement.models import ReservedGroupName
from django.contrib import admin, messages
from django.db.models import Count, Prefetch
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html

from .forms import MiningAccessGroupForm, mining_permissions
from .models import MiningAccessGroup


@admin.register(MiningAccessGroup)
class MiningAccessGroupAdmin(admin.ModelAdmin):
    """Manage only this app's permissions on existing Auth groups."""

    form = MiningAccessGroupForm
    ordering = ("name",)
    search_fields = ("name",)
    list_display = (
        "name",
        "member_count",
        "own_access",
        "corporation_access",
        "site_wide_access",
        "csv_export",
        "admin_access",
        "full_group_link",
    )
    fields = (
        "name",
        "adopt_reserved_role",
        "members",
        "mining_permissions",
        "permission_guide",
        "full_group_link",
    )
    readonly_fields = ("permission_guide", "full_group_link")
    save_on_top = True
    actions = ("remove_mining_analytics_access",)

    def _can_manage(self, request):
        return request.user.is_superuser or request.user.has_perm(
            "buh_mining_analytics.manage_access"
        )

    def has_module_permission(self, request):
        return self._can_manage(request)

    def has_view_permission(self, request, obj=None):
        del obj
        return self._can_manage(request)

    def has_change_permission(self, request, obj=None):
        del obj
        return self._can_manage(request)

    def has_delete_permission(self, request, obj=None):
        del request, obj
        return False

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(mining_member_count=Count("user", distinct=True))
            .prefetch_related(
                Prefetch(
                    "permissions",
                    queryset=mining_permissions(),
                    to_attr="admin_mining_permissions",
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
        available = list(mining_permissions())
        selected_ids = {
            permission.pk for permission in form.cleaned_data["mining_permissions"]
        }
        obj.permissions.remove(
            *(permission for permission in available if permission.pk not in selected_ids)
        )
        obj.permissions.add(
            *(permission for permission in available if permission.pk in selected_ids)
        )

    @admin.action(
        description=(
            "Remove Mining Analytics access only "
            "(preserve Auth group and Discord role)"
        )
    )
    def remove_mining_analytics_access(self, request, queryset):
        available = list(mining_permissions())
        for group in queryset:
            group.permissions.remove(*available)
        self.message_user(
            request,
            f"Removed Mining Analytics permissions from {queryset.count()} group(s). "
            "Auth groups, memberships, Discord roles, and unrelated permissions "
            "were preserved.",
            messages.SUCCESS,
        )

    @staticmethod
    def _codes(obj):
        prefetched = getattr(obj, "admin_mining_permissions", None)
        if prefetched is not None:
            return {permission.codename for permission in prefetched}
        return set(
            obj.permissions.filter(
                content_type__app_label="buh_mining_analytics"
            ).values_list("codename", flat=True)
        )

    @admin.display(description="Members", ordering="mining_member_count")
    def member_count(self, obj):
        return getattr(obj, "mining_member_count", obj.user_set.count())

    @admin.display(boolean=True, description="Own characters")
    def own_access(self, obj):
        return "basic_access" in self._codes(obj)

    @admin.display(boolean=True, description="Corporation")
    def corporation_access(self, obj):
        return "view_corporation" in self._codes(obj)

    @admin.display(boolean=True, description="Site-wide")
    def site_wide_access(self, obj):
        return "view_all" in self._codes(obj)

    @admin.display(boolean=True, description="CSV export")
    def csv_export(self, obj):
        return "export_data" in self._codes(obj)

    @admin.display(boolean=True, description="Manage access")
    def admin_access(self, obj):
        return "manage_access" in self._codes(obj)

    @admin.display(description="Full group editor")
    def full_group_link(self, obj):
        if not obj or not obj.pk:
            return "Save the group before opening the full editor."
        try:
            url = reverse("admin:groupmanagement_group_change", args=(obj.pk,))
        except NoReverseMatch:
            return "Alliance Auth group editor unavailable"
        return format_html('<a href="{}">Open full Alliance Auth group</a>', url)

    @admin.display(description="Permission guide")
    def permission_guide(self, obj):
        del obj
        return format_html(
            "<ul>"
            "<li><strong>{}</strong> — {}</li>"
            "<li><strong>{}</strong> — {}</li>"
            "<li><strong>{}</strong> — {}</li>"
            "<li><strong>{}</strong> — {}</li>"
            "<li><strong>{}</strong> — {}</li>"
            "</ul>",
            "Basic access",
            "Open the dashboard and view characters owned by the Auth account.",
            "View corporation",
            "View corporation members, their registered alts, and current in-corp characters.",
            "View all",
            "View every registered Member Audit character on the Auth site.",
            "Export data",
            "Download CSV data, limited to the user's allowed scope and selection.",
            "Manage access",
            "Open this Django admin section and edit Mining Analytics group permissions.",
        )
