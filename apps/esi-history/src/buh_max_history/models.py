from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.timezone import now


class HistoryArchivePermission(models.Model):
    """Unmanaged model used only to create granular archive permissions."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("view_history_archive", "Can open ESI History Archive"),
            ("view_archive_storage", "Can view archive storage totals"),
            ("view_archive_coverage", "Can view endpoint and dataset coverage"),
            ("view_public_archive", "Can view public archive metadata"),
            ("view_private_archive_metadata", "Can view private ESI archive metadata"),
            ("download_public_archive", "Can download archived public payloads"),
            ("download_private_archive", "Can download archived private payloads"),
            ("run_public_archive_sync", "Can catalog and synchronize public datasets"),
            ("manage_archive_settings", "Can configure ESI History Archive"),
            ("manage_archive_access", "Can administer ESI History Archive access"),
        )


class HistoryArchiveAccessGroup(Group):
    class Meta:
        proxy = True
        default_permissions = ()
        verbose_name = "ESI History Archive access group"
        verbose_name_plural = "ESI History Archive access groups"


class ArchiveConfiguration(models.Model):
    singleton_id = models.PositiveSmallIntegerField(
        primary_key=True, default=1, editable=False
    )
    capture_enabled = models.BooleanField(default=True)
    capture_public_esi = models.BooleanField(default=True)
    capture_private_esi = models.BooleanField(default=True)
    max_response_mib = models.PositiveIntegerField(
        default=512,
        validators=[MinValueValidator(1), MaxValueValidator(2048)],
        help_text="Responses above this size are recorded as skipped metadata, not payload files.",
    )
    minimum_free_gib = models.PositiveIntegerField(
        default=25,
        validators=[MinValueValidator(5), MaxValueValidator(2048)],
        help_text="Archiving pauses before the host drops below this much free space.",
    )
    public_mirror_enabled = models.BooleanField(default=True)
    discover_public_datasets = models.BooleanField(default=True, db_default=True)
    active_esi_enabled = models.BooleanField(default=True, db_default=True)
    local_history_enabled = models.BooleanField(default=True, db_default=True)
    active_requests_per_run = models.PositiveSmallIntegerField(
        default=120,
        db_default=120,
        validators=[MinValueValidator(1), MaxValueValidator(2000)],
    )
    local_rows_per_run = models.PositiveIntegerField(default=500, db_default=500)
    discovery_character_cursor = models.PositiveBigIntegerField(default=0, db_default=0)
    last_public_discovery_at = models.DateTimeField(null=True, blank=True)
    active_retry_at = models.DateTimeField(null=True, blank=True)
    public_retry_at = models.DateTimeField(null=True, blank=True)
    public_max_files_per_run = models.PositiveSmallIntegerField(
        default=12, validators=[MinValueValidator(1), MaxValueValidator(250)]
    )
    public_max_gib_per_run = models.PositiveSmallIntegerField(
        default=4, validators=[MinValueValidator(1), MaxValueValidator(250)]
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "ESI History Archive configuration"
        verbose_name_plural = "ESI History Archive configuration"

    def save(self, *args, **kwargs):
        self.singleton_id = 1
        return super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(singleton_id=1)
        return obj

    def __str__(self):
        return "ESI History Archive configuration"


class ArchiveStream(models.Model):
    stream_key = models.CharField(max_length=64, unique=True)
    operation_id = models.CharField(max_length=180, db_index=True)
    method = models.CharField(max_length=12, default="GET")
    url_template = models.CharField(max_length=500)
    safe_parameters = models.JSONField(default=dict, blank=True)
    app_name = models.CharField(max_length=160, blank=True, default="")
    is_private = models.BooleanField(default=False, db_index=True)
    character_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    auth_user_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    request_count = models.PositiveBigIntegerField(default=0)
    snapshot_count = models.PositiveIntegerField(default=0)
    source_bytes = models.PositiveBigIntegerField(default=0)
    stored_bytes = models.PositiveBigIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    current_payload_sha256 = models.CharField(max_length=64, blank=True, default="")
    last_status_code = models.PositiveSmallIntegerField(default=0)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ("-last_seen_at",)
        indexes = [
            models.Index(fields=("is_private", "-last_seen_at")),
            models.Index(fields=("operation_id", "-last_seen_at")),
        ]

    def __str__(self):
        subject = f"character {self.character_id}" if self.character_id else "public"
        return f"{self.operation_id} · {subject}"


class ArchiveSnapshot(models.Model):
    stream = models.ForeignKey(
        ArchiveStream, on_delete=models.CASCADE, related_name="snapshots"
    )
    payload_sha256 = models.CharField(max_length=64)
    relative_path = models.CharField(max_length=700)
    content_type = models.CharField(max_length=120, blank=True, default="")
    status_code = models.PositiveSmallIntegerField(default=200)
    source_bytes = models.PositiveBigIntegerField(default=0)
    stored_bytes = models.PositiveBigIntegerField(default=0)
    response_headers = models.JSONField(default=dict, blank=True)
    first_observed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_observed_at = models.DateTimeField(auto_now=True)
    observation_count = models.PositiveBigIntegerField(default=1)

    class Meta:
        ordering = ("-first_observed_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("stream", "payload_sha256"),
                name="buh_history_unique_stream_payload",
            )
        ]
        indexes = [models.Index(fields=("stream", "-first_observed_at"))]

    def __str__(self):
        return f"{self.stream.operation_id} · {self.payload_sha256[:12]}"


class ArchiveObservation(models.Model):
    """Consecutive observations of one state; A -> B -> A retains three spans."""

    stream = models.ForeignKey(
        ArchiveStream, on_delete=models.CASCADE, related_name="observations"
    )
    snapshot = models.ForeignKey(
        ArchiveSnapshot, on_delete=models.PROTECT, related_name="observations"
    )
    first_observed_at = models.DateTimeField(default=now, db_index=True)
    last_observed_at = models.DateTimeField(default=now)
    observation_count = models.PositiveBigIntegerField(default=1)

    class Meta:
        ordering = ("-pk",)
        indexes = [models.Index(fields=("stream", "-id"))]


class ArchiveCollectionTarget(models.Model):
    """Durable, credential-free cursor and coverage state for one collector."""

    key = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=12, default="esi", db_index=True)
    operation_id = models.CharField(max_length=180, db_index=True)
    parameters = models.JSONField(default=dict)
    character_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    corporation_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    status = models.CharField(max_length=32, default="pending", db_index=True)
    cursor = models.JSONField(default=dict, blank=True)
    next_attempt_at = models.DateTimeField(default=now, db_index=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failure_count = models.PositiveIntegerField(default=0)
    detail = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ("next_attempt_at", "pk")


class ArchiveCaptureIssue(models.Model):
    operation_id = models.CharField(max_length=180, blank=True, default="")
    reason = models.CharField(max_length=80, db_index=True)
    detail = models.CharField(max_length=500, blank=True, default="")
    occurrences = models.PositiveIntegerField(default=1)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ("-last_seen_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("operation_id", "reason", "detail"),
                name="buh_history_unique_capture_issue",
            )
        ]

    def __str__(self):
        return f"{self.operation_id or 'archive'}: {self.reason}"


class PublicDataset(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, unique=True)
    index_url = models.URLField(max_length=700, unique=True)
    enabled = models.BooleanField(default=True, db_index=True)
    priority = models.PositiveSmallIntegerField(default=100, db_index=True)
    download_turn = models.PositiveSmallIntegerField(default=0, db_default=0)
    last_catalog_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ("priority", "name")

    def __str__(self):
        return self.name


class PublicCatalogIndex(models.Model):
    """One resumable node in an EVE Ref directory-index tree."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CATALOGED = "CATALOGED", "Cataloged"
        FAILED = "FAILED", "Failed"

    dataset = models.ForeignKey(
        PublicDataset, on_delete=models.CASCADE, related_name="indexes"
    )
    source_url = models.URLField(max_length=900)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    depth = models.PositiveSmallIntegerField(default=0)
    cursor = models.PositiveIntegerField(default=0, db_default=0)
    catalog_sha256 = models.CharField(max_length=64, blank=True, db_default="")
    discovered_at = models.DateTimeField(auto_now_add=True)
    last_catalog_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_checked_at = models.DateTimeField(auto_now=True)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ("depth", "source_url")
        constraints = [
            models.UniqueConstraint(
                fields=("dataset", "source_url"),
                name="buh_history_unique_dataset_index",
            )
        ]
        indexes = [models.Index(fields=("dataset", "status", "last_catalog_at"))]

    def __str__(self):
        return self.source_url


class PublicArchiveFile(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DOWNLOADING = "DOWNLOADING", "Downloading"
        STORED = "STORED", "Stored"
        SKIPPED = "SKIPPED", "Skipped"
        FAILED = "FAILED", "Failed"

    dataset = models.ForeignKey(
        PublicDataset, on_delete=models.CASCADE, related_name="files"
    )
    source_url = models.URLField(max_length=900, unique=True)
    relative_path = models.CharField(max_length=700)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    remote_size = models.PositiveBigIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    failure_count = models.PositiveIntegerField(default=0, db_default=0)
    stored_bytes = models.PositiveBigIntegerField(default=0)
    payload_sha256 = models.CharField(max_length=64, blank=True, default="")
    etag = models.CharField(max_length=300, blank=True, default="")
    remote_modified = models.CharField(max_length=120, blank=True, default="")
    source_time = models.DateTimeField(null=True, blank=True, db_index=True)
    discovered_at = models.DateTimeField(auto_now_add=True)
    downloaded_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(auto_now=True)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ("dataset__priority", "source_url")
        indexes = [models.Index(fields=("status", "dataset"))]

    def __str__(self):
        return self.source_url


class ArchiveJob(models.Model):
    class Kind(models.TextChoices):
        CATALOG = "CATALOG", "Catalog public datasets"
        SYNC = "SYNC", "Synchronize public datasets"
        VERIFY = "VERIFY", "Verify archive files"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=16, choices=Kind.choices, db_index=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="buh_archive_jobs",
    )
    message = models.TextField(blank=True, default="")
    result = models.JSONField(default=dict, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-requested_at",)

    def __str__(self):
        return f"{self.get_kind_display()} · {self.get_status_display()}"


class PublicArchiveRevision(models.Model):
    """A dated state of a public file; payload paths are content addressed."""

    file = models.ForeignKey(
        PublicArchiveFile, on_delete=models.PROTECT, related_name="revisions"
    )
    payload_sha256 = models.CharField(max_length=64)
    relative_path = models.CharField(max_length=700)
    stored_bytes = models.PositiveBigIntegerField(default=0)
    first_observed_at = models.DateTimeField(default=now, db_index=True)
    last_observed_at = models.DateTimeField(default=now)

    class Meta:
        ordering = ("-pk",)
        indexes = [models.Index(fields=("file", "-id"))]
