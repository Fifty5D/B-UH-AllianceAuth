from allianceauth.groupmanagement.models import ReservedGroupName
from django.contrib import admin, messages
from django.db import models
from django.db.models import Count, Prefetch

from .forms import (
    ArchiveConfigurationForm,
    HistoryArchiveAccessGroupForm,
    PublicDatasetForm,
    archive_permissions,
)
from .models import (
    ArchiveCaptureIssue,
    ArchiveConfiguration,
    ArchiveJob,
    ArchiveSnapshot,
    ArchiveStream,
    HistoryArchiveAccessGroup,
    PublicArchiveFile,
    PublicCatalogIndex,
    PublicDataset,
)


class ArchivePermissionAdminMixin:
    permission = "manage_archive_settings"

    def _allowed(self, request):
        return request.user.is_superuser or request.user.has_perm(
            f"buh_max_history.{self.permission}"
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


@admin.register(HistoryArchiveAccessGroup)
class HistoryArchiveAccessGroupAdmin(ArchivePermissionAdminMixin, admin.ModelAdmin):
    permission = "manage_archive_access"
    form = HistoryArchiveAccessGroupForm
    ordering = ("name",)
    search_fields = ("name", "user__username")
    list_display = (
        "name",
        "member_count",
        "archive_view",
        "private_metadata",
        "private_download",
        "sync_control",
        "settings_control",
    )
    fields = ("name", "adopt_reserved_role", "members", "archive_permissions")
    save_on_top = True
    actions = ("remove_archive_access",)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(archive_member_count=Count("user", distinct=True))
            .prefetch_related(
                Prefetch(
                    "permissions",
                    queryset=archive_permissions(),
                    to_attr="admin_archive_permissions",
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
        available = list(archive_permissions())
        selected = {item.pk for item in form.cleaned_data["archive_permissions"]}
        obj.permissions.remove(*(item for item in available if item.pk not in selected))
        obj.permissions.add(*(item for item in available if item.pk in selected))

    @admin.action(
        description="Remove ESI Archive access only; preserve the Auth/Discord group"
    )
    def remove_archive_access(self, request, queryset):
        permissions = list(archive_permissions())
        for group in queryset:
            group.permissions.remove(*permissions)
        self.message_user(
            request,
            f"Removed ESI Archive permissions from {queryset.count()} group(s).",
            messages.SUCCESS,
        )

    @staticmethod
    def _codes(obj):
        return {
            item.codename
            for item in getattr(obj, "admin_archive_permissions", obj.permissions.all())
        }

    @admin.display(description="Members", ordering="archive_member_count")
    def member_count(self, obj):
        return getattr(obj, "archive_member_count", obj.user_set.count())

    @admin.display(boolean=True, description="Archive")
    def archive_view(self, obj):
        return "view_history_archive" in self._codes(obj)

    @admin.display(boolean=True, description="Private metadata")
    def private_metadata(self, obj):
        return "view_private_archive_metadata" in self._codes(obj)

    @admin.display(boolean=True, description="Private payload")
    def private_download(self, obj):
        return "download_private_archive" in self._codes(obj)

    @admin.display(boolean=True, description="Public sync")
    def sync_control(self, obj):
        return "run_public_archive_sync" in self._codes(obj)

    @admin.display(boolean=True, description="Settings")
    def settings_control(self, obj):
        return "manage_archive_settings" in self._codes(obj)


@admin.register(ArchiveConfiguration)
class ArchiveConfigurationAdmin(ArchivePermissionAdminMixin, admin.ModelAdmin):
    form = ArchiveConfigurationForm
    fieldsets = (
        (
            "Change-only ESI capture",
            {
                "fields": (
                    "capture_enabled",
                    "capture_public_esi",
                    "capture_private_esi",
                    "max_response_mib",
                    "minimum_free_gib",
                )
            },
        ),
        (
            "EVE Ref public mirror",
            {
                "fields": (
                    "public_mirror_enabled",
                    "public_max_files_per_run",
                    "public_max_gib_per_run",
                )
            },
        ),
        ("Change record", {"fields": ("updated_by", "updated_at")}),
    )
    readonly_fields = ("updated_by", "updated_at")

    def has_add_permission(self, request):
        return self._allowed(request) and not ArchiveConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(PublicDataset)
class PublicDatasetAdmin(ArchivePermissionAdminMixin, admin.ModelAdmin):
    form = PublicDatasetForm
    list_display = (
        "name",
        "enabled",
        "priority",
        "file_count",
        "stored_count",
        "last_catalog_at",
        "last_sync_at",
    )
    list_filter = ("enabled",)
    search_fields = ("name", "slug", "index_url")
    ordering = ("priority", "name")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                admin_file_count=Count("files"),
                admin_stored_count=Count(
                    "files", filter=models.Q(files__status=PublicArchiveFile.Status.STORED)
                ),
            )
        )

    @admin.display(description="Files", ordering="admin_file_count")
    def file_count(self, obj):
        return obj.admin_file_count

    @admin.display(description="Stored", ordering="admin_stored_count")
    def stored_count(self, obj):
        return obj.admin_stored_count


@admin.register(ArchiveStream)
class ArchiveStreamAdmin(ArchivePermissionAdminMixin, admin.ModelAdmin):
    list_display = (
        "operation_id",
        "is_private",
        "character_id",
        "request_count",
        "snapshot_count",
        "stored_bytes",
        "last_seen_at",
    )
    list_filter = ("is_private", "method")
    search_fields = ("operation_id", "character_id", "stream_key")
    readonly_fields = tuple(field.name for field in ArchiveStream._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return self._allowed(request) and obj is None

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ArchiveSnapshot)
class ArchiveSnapshotAdmin(ArchivePermissionAdminMixin, admin.ModelAdmin):
    list_display = (
        "first_observed_at",
        "stream",
        "source_bytes",
        "stored_bytes",
        "observation_count",
    )
    search_fields = ("stream__operation_id", "payload_sha256", "relative_path")
    readonly_fields = tuple(field.name for field in ArchiveSnapshot._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return self._allowed(request) and obj is None

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PublicArchiveFile)
class PublicArchiveFileAdmin(ArchivePermissionAdminMixin, admin.ModelAdmin):
    list_display = (
        "dataset",
        "status",
        "remote_size",
        "stored_bytes",
        "downloaded_at",
    )
    list_filter = ("dataset", "status")
    search_fields = ("source_url", "relative_path", "payload_sha256")
    readonly_fields = tuple(field.name for field in PublicArchiveFile._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return self._allowed(request) and obj is None

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PublicCatalogIndex)
class PublicCatalogIndexAdmin(ArchivePermissionAdminMixin, admin.ModelAdmin):
    list_display = ("dataset", "status", "depth", "last_catalog_at", "source_url")
    list_filter = ("dataset", "status")
    search_fields = ("source_url", "last_error")
    readonly_fields = tuple(field.name for field in PublicCatalogIndex._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return self._allowed(request) and obj is None

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ArchiveJob)
class ArchiveJobAdmin(ArchivePermissionAdminMixin, admin.ModelAdmin):
    list_display = ("requested_at", "kind", "requested_by", "status", "finished_at")
    list_filter = ("kind", "status")
    search_fields = ("requested_by__username", "message")
    readonly_fields = tuple(field.name for field in ArchiveJob._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return self._allowed(request) and obj is None

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ArchiveCaptureIssue)
class ArchiveCaptureIssueAdmin(ArchivePermissionAdminMixin, admin.ModelAdmin):
    list_display = ("last_seen_at", "operation_id", "reason", "occurrences")
    list_filter = ("reason",)
    search_fields = ("operation_id", "detail")
    readonly_fields = tuple(field.name for field in ArchiveCaptureIssue._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return self._allowed(request) and obj is None

    def has_delete_permission(self, request, obj=None):
        return False
