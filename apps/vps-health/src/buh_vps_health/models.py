import uuid

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class VpsHealthPermission(models.Model):
    """Unmanaged model used only to create granular application permissions."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("view_vps_health", "Can open VPS Health"),
            ("view_host_metrics", "Can view VPS resource metrics"),
            ("view_container_metrics", "Can view Docker container metrics"),
            ("view_worker_workload", "Can view Celery worker workload"),
            ("view_task_queue", "Can view Celery queue details"),
            ("view_action_history", "Can view guarded operation history"),
            ("check_updates", "Can run a read-only update scan"),
            ("view_update_results", "Can view detailed update scan results"),
            ("restart_auth_services", "Can restart Alliance Auth services"),
            ("scale_workers", "Can add or remove Alliance Auth workers"),
            ("manage_health_settings", "Can configure health thresholds and alerts"),
            ("manage_health_access", "Can administer VPS Health access"),
        )


class VpsHealthAccessGroup(Group):
    class Meta:
        proxy = True
        default_permissions = ()
        verbose_name = "VPS Health access group"
        verbose_name_plural = "VPS Health access groups"


class HealthConfiguration(models.Model):
    singleton_id = models.PositiveSmallIntegerField(
        primary_key=True, default=1, editable=False
    )
    live_refresh_seconds = models.PositiveSmallIntegerField(
        default=5, validators=[MinValueValidator(3), MaxValueValidator(60)]
    )
    history_retention_days = models.PositiveSmallIntegerField(
        default=30, validators=[MinValueValidator(1), MaxValueValidator(365)]
    )
    snapshot_interval_minutes = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(60)]
    )
    warning_cpu_percent = models.PositiveSmallIntegerField(
        default=85, validators=[MinValueValidator(1), MaxValueValidator(100)]
    )
    warning_memory_percent = models.PositiveSmallIntegerField(
        default=85, validators=[MinValueValidator(1), MaxValueValidator(100)]
    )
    warning_disk_percent = models.PositiveSmallIntegerField(
        default=85, validators=[MinValueValidator(1), MaxValueValidator(100)]
    )
    warning_load_per_cpu = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=1.50,
        validators=[MinValueValidator(0.25), MaxValueValidator(10)],
    )
    warning_queue_depth = models.PositiveIntegerField(default=500)
    alert_sustain_minutes = models.PositiveSmallIntegerField(
        default=5, validators=[MinValueValidator(1), MaxValueValidator(120)]
    )
    notify_directors = models.BooleanField(default=True)
    worker_minimum = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(8)]
    )
    worker_maximum = models.PositiveSmallIntegerField(
        default=8, validators=[MinValueValidator(1), MaxValueValidator(8)]
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
        verbose_name = "VPS Health configuration"
        verbose_name_plural = "VPS Health configuration"

    def clean(self):
        super().clean()
        if self.worker_minimum > self.worker_maximum:
            from django.core.exceptions import ValidationError

            raise ValidationError("Worker minimum cannot exceed the worker maximum.")

    def save(self, *args, **kwargs):
        self.singleton_id = 1
        return super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(singleton_id=1)
        return obj

    def __str__(self):
        return "VPS Health configuration"


class MetricSnapshot(models.Model):
    captured_at = models.DateTimeField(auto_now_add=True, db_index=True)
    cpu_percent = models.FloatField(default=0)
    cpu_count = models.PositiveSmallIntegerField(default=1)
    load_1 = models.FloatField(default=0)
    load_5 = models.FloatField(default=0)
    load_15 = models.FloatField(default=0)
    memory_percent = models.FloatField(default=0)
    memory_used_bytes = models.PositiveBigIntegerField(default=0)
    memory_total_bytes = models.PositiveBigIntegerField(default=0)
    swap_percent = models.FloatField(default=0)
    disk_percent = models.FloatField(default=0)
    disk_used_bytes = models.PositiveBigIntegerField(default=0)
    disk_total_bytes = models.PositiveBigIntegerField(default=0)
    network_rx_bytes_per_second = models.FloatField(default=0)
    network_tx_bytes_per_second = models.FloatField(default=0)
    container_total = models.PositiveSmallIntegerField(default=0)
    container_unhealthy = models.PositiveSmallIntegerField(default=0)
    worker_desired = models.PositiveSmallIntegerField(default=0)
    worker_online = models.PositiveSmallIntegerField(default=0)
    active_tasks = models.PositiveIntegerField(default=0)
    queued_tasks = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-captured_at",)
        indexes = [models.Index(fields=("-captured_at",))]

    def __str__(self):
        return f"VPS snapshot at {self.captured_at}"


class OperationalAction(models.Model):
    class Action(models.TextChoices):
        RESTART_AUTH = "RESTART_AUTH", "Restart Auth services"
        SET_WORKERS = "SET_WORKERS", "Set worker count"
        CHECK_UPDATES = "CHECK_UPDATES", "Check all updates"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        ACCEPTED = "ACCEPTED", "Accepted by host agent"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    action = models.CharField(max_length=24, choices=Action.choices, db_index=True)
    target_worker_count = models.PositiveSmallIntegerField(null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="buh_vps_health_actions",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    agent_action_id = models.CharField(max_length=64, blank=True, default="")
    message = models.TextField(blank=True, default="")
    result_payload = models.JSONField(default=dict, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-requested_at",)

    def __str__(self):
        return f"{self.get_action_display()} by {self.requested_by}"


class AlertState(models.Model):
    class Severity(models.TextChoices):
        WARNING = "warning", "Warning"
        DANGER = "danger", "Critical"
        SUCCESS = "success", "Recovered"

    key = models.CharField(max_length=80, unique=True)
    title = models.CharField(max_length=180)
    message = models.TextField()
    severity = models.CharField(max_length=12, choices=Severity.choices)
    active = models.BooleanField(default=True, db_index=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    notification_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-active", "-last_seen_at")

    def __str__(self):
        return self.title
