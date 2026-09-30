import uuid

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.CreateModel(
            name="VpsHealthPermission",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
            ],
            options={
                "permissions": (
                    ("view_vps_health", "Can open VPS Health"),
                    ("view_host_metrics", "Can view VPS resource metrics"),
                    ("view_container_metrics", "Can view Docker container metrics"),
                    ("view_worker_workload", "Can view Celery worker workload"),
                    ("view_task_queue", "Can view Celery queue details"),
                    ("view_action_history", "Can view restart and scaling history"),
                    ("restart_auth_services", "Can restart Alliance Auth services"),
                    ("scale_workers", "Can add or remove Alliance Auth workers"),
                    (
                        "manage_health_settings",
                        "Can configure health thresholds and alerts",
                    ),
                    ("manage_health_access", "Can administer VPS Health access"),
                ),
                "managed": False,
                "default_permissions": (),
            },
        ),
        migrations.CreateModel(
            name="AlertState",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("key", models.CharField(max_length=80, unique=True)),
                ("title", models.CharField(max_length=180)),
                ("message", models.TextField()),
                (
                    "severity",
                    models.CharField(
                        choices=[
                            ("warning", "Warning"),
                            ("danger", "Critical"),
                            ("success", "Recovered"),
                        ],
                        max_length=12,
                    ),
                ),
                ("active", models.BooleanField(db_index=True, default=True)),
                ("first_seen_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen_at", models.DateTimeField(auto_now=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("notification_sent_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ("-active", "-last_seen_at")},
        ),
        migrations.CreateModel(
            name="MetricSnapshot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("captured_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("cpu_percent", models.FloatField(default=0)),
                ("cpu_count", models.PositiveSmallIntegerField(default=1)),
                ("load_1", models.FloatField(default=0)),
                ("load_5", models.FloatField(default=0)),
                ("load_15", models.FloatField(default=0)),
                ("memory_percent", models.FloatField(default=0)),
                ("memory_used_bytes", models.PositiveBigIntegerField(default=0)),
                ("memory_total_bytes", models.PositiveBigIntegerField(default=0)),
                ("swap_percent", models.FloatField(default=0)),
                ("disk_percent", models.FloatField(default=0)),
                ("disk_used_bytes", models.PositiveBigIntegerField(default=0)),
                ("disk_total_bytes", models.PositiveBigIntegerField(default=0)),
                ("network_rx_bytes_per_second", models.FloatField(default=0)),
                ("network_tx_bytes_per_second", models.FloatField(default=0)),
                ("container_total", models.PositiveSmallIntegerField(default=0)),
                ("container_unhealthy", models.PositiveSmallIntegerField(default=0)),
                ("worker_desired", models.PositiveSmallIntegerField(default=0)),
                ("worker_online", models.PositiveSmallIntegerField(default=0)),
                ("active_tasks", models.PositiveIntegerField(default=0)),
                ("queued_tasks", models.PositiveIntegerField(default=0)),
            ],
            options={"ordering": ("-captured_at",)},
        ),
        migrations.CreateModel(
            name="HealthConfiguration",
            fields=[
                (
                    "singleton_id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "live_refresh_seconds",
                    models.PositiveSmallIntegerField(
                        default=5,
                        validators=[
                            django.core.validators.MinValueValidator(3),
                            django.core.validators.MaxValueValidator(60),
                        ],
                    ),
                ),
                (
                    "history_retention_days",
                    models.PositiveSmallIntegerField(
                        default=30,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(365),
                        ],
                    ),
                ),
                (
                    "snapshot_interval_minutes",
                    models.PositiveSmallIntegerField(
                        default=1,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(60),
                        ],
                    ),
                ),
                (
                    "warning_cpu_percent",
                    models.PositiveSmallIntegerField(
                        default=85,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(100),
                        ],
                    ),
                ),
                (
                    "warning_memory_percent",
                    models.PositiveSmallIntegerField(
                        default=85,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(100),
                        ],
                    ),
                ),
                (
                    "warning_disk_percent",
                    models.PositiveSmallIntegerField(
                        default=85,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(100),
                        ],
                    ),
                ),
                (
                    "warning_load_per_cpu",
                    models.DecimalField(
                        decimal_places=2,
                        default=1.5,
                        max_digits=4,
                        validators=[
                            django.core.validators.MinValueValidator(0.25),
                            django.core.validators.MaxValueValidator(10),
                        ],
                    ),
                ),
                ("warning_queue_depth", models.PositiveIntegerField(default=500)),
                (
                    "alert_sustain_minutes",
                    models.PositiveSmallIntegerField(
                        default=5,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(120),
                        ],
                    ),
                ),
                ("notify_directors", models.BooleanField(default=True)),
                (
                    "worker_minimum",
                    models.PositiveSmallIntegerField(
                        default=1,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(8),
                        ],
                    ),
                ),
                (
                    "worker_maximum",
                    models.PositiveSmallIntegerField(
                        default=8,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(8),
                        ],
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "VPS Health configuration",
                "verbose_name_plural": "VPS Health configuration",
            },
        ),
        migrations.CreateModel(
            name="OperationalAction",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("RESTART_AUTH", "Restart Auth services"),
                            ("SET_WORKERS", "Set worker count"),
                        ],
                        db_index=True,
                        max_length=24,
                    ),
                ),
                (
                    "target_worker_count",
                    models.PositiveSmallIntegerField(blank=True, null=True),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("QUEUED", "Queued"),
                            ("ACCEPTED", "Accepted by host agent"),
                            ("RUNNING", "Running"),
                            ("SUCCEEDED", "Succeeded"),
                            ("FAILED", "Failed"),
                        ],
                        db_index=True,
                        default="QUEUED",
                        max_length=16,
                    ),
                ),
                (
                    "agent_action_id",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("message", models.TextField(blank=True, default="")),
                (
                    "requested_at",
                    models.DateTimeField(auto_now_add=True, db_index=True),
                ),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="buh_vps_health_actions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("-requested_at",)},
        ),
        migrations.CreateModel(
            name="VpsHealthAccessGroup",
            fields=[],
            options={
                "verbose_name": "VPS Health access group",
                "verbose_name_plural": "VPS Health access groups",
                "proxy": True,
                "indexes": [],
                "constraints": [],
                "default_permissions": (),
            },
            bases=("auth.group",),
        ),
        migrations.AddIndex(
            model_name="metricsnapshot",
            index=models.Index(
                fields=["-captured_at"], name="buh_vps_hea_capture_35466a_idx"
            ),
        ),
    ]
