# Generated for B-UH Structure Operations 0.1.0.

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
            name="StructureOpsPermission",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                )
            ],
            options={
                "permissions": (
                    ("view_structure_ops", "Can view Structure Operations"),
                    ("manage_structure_ops", "Can manage Structure Operations"),
                    (
                        "admin_structure_ops",
                        "Can administer Structure Operations access and setup",
                    ),
                ),
                "managed": False,
                "default_permissions": (),
            },
        ),
        migrations.CreateModel(
            name="StructureOpsAccessGroup",
            fields=[],
            options={
                "verbose_name": "Structure Operations access group",
                "verbose_name_plural": "Structure Operations access groups",
                "proxy": True,
                "indexes": [],
                "constraints": [],
                "default_permissions": (),
            },
            bases=("auth.group",),
        ),
        migrations.CreateModel(
            name="TrackedCorporation",
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
                ("corporation_id", models.PositiveBigIntegerField(unique=True)),
                ("corporation_name", models.CharField(max_length=255)),
                ("is_required", models.BooleanField(default=True)),
                ("is_enabled", models.BooleanField(default=True)),
                ("notes", models.CharField(blank=True, default="", max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "tracked corporation",
                "verbose_name_plural": "tracked corporations",
                "ordering": ("corporation_name",),
            },
        ),
        migrations.CreateModel(
            name="StructurePreference",
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
                ("structure_id", models.PositiveBigIntegerField(unique=True)),
                (
                    "target_fuel_days",
                    models.PositiveSmallIntegerField(
                        default=30,
                        validators=[
                            django.core.validators.MinValueValidator(7),
                            django.core.validators.MaxValueValidator(180),
                        ],
                    ),
                ),
                ("notes", models.TextField(blank=True, default="")),
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
                "verbose_name": "structure preference",
                "verbose_name_plural": "structure preferences",
                "ordering": ("structure_id",),
            },
        ),
        migrations.CreateModel(
            name="StructureSnapshot",
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
                ("structure_id", models.PositiveBigIntegerField(db_index=True)),
                ("corporation_id", models.PositiveBigIntegerField(db_index=True)),
                ("structure_name", models.CharField(max_length=255)),
                ("captured_at", models.DateTimeField(db_index=True)),
                ("fuel_expires_at", models.DateTimeField(blank=True, null=True)),
                ("fuel_quantity", models.BigIntegerField(blank=True, null=True)),
                (
                    "fuel_blocks_per_day",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                ("state", models.CharField(blank=True, default="", max_length=80)),
                ("service_count", models.PositiveSmallIntegerField(default=0)),
            ],
            options={"ordering": ("-captured_at",)},
        ),
        migrations.CreateModel(
            name="AlertEvent",
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
                ("event_key", models.CharField(max_length=255, unique=True)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("fuel", "Fuel"),
                            ("extraction", "Extraction"),
                            ("attack", "Attack / reinforcement"),
                            ("war", "War"),
                            ("sync", "Token / synchronization"),
                        ],
                        db_index=True,
                        max_length=16,
                    ),
                ),
                (
                    "severity",
                    models.CharField(
                        choices=[
                            ("info", "Info"),
                            ("success", "Success"),
                            ("warning", "Warning"),
                            ("danger", "Critical"),
                        ],
                        db_index=True,
                        max_length=12,
                    ),
                ),
                (
                    "corporation_id",
                    models.PositiveBigIntegerField(
                        blank=True, db_index=True, null=True
                    ),
                ),
                (
                    "structure_id",
                    models.PositiveBigIntegerField(
                        blank=True, db_index=True, null=True
                    ),
                ),
                ("title", models.CharField(max_length=254)),
                ("message", models.TextField()),
                ("source_id", models.CharField(blank=True, default="", max_length=100)),
                ("occurred_at", models.DateTimeField(db_index=True)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("occurrence_count", models.PositiveIntegerField(default=1)),
                ("last_seen_at", models.DateTimeField(auto_now=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("acknowledged_at", models.DateTimeField(blank=True, null=True)),
                ("notification_sent_at", models.DateTimeField(blank=True, null=True)),
                (
                    "acknowledged_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("-occurred_at",)},
        ),
        migrations.CreateModel(
            name="WarState",
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
                ("signature", models.CharField(max_length=255, unique=True)),
                ("corporation_id", models.PositiveBigIntegerField(db_index=True)),
                ("aggressor_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("defender_id", models.PositiveBigIntegerField(blank=True, null=True)),
                (
                    "war_hq_structure_id",
                    models.PositiveBigIntegerField(blank=True, null=True),
                ),
                (
                    "war_hq_name",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("declared_at", models.DateTimeField(blank=True, null=True)),
                ("ends_at", models.DateTimeField(blank=True, null=True)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("last_event_type", models.CharField(max_length=100)),
                (
                    "last_notification_id",
                    models.PositiveBigIntegerField(blank=True, null=True),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ("-is_active", "-updated_at")},
        ),
        migrations.AddIndex(
            model_name="structuresnapshot",
            index=models.Index(
                fields=["structure_id", "-captured_at"],
                name="buh_structu_structu_072a3a_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="structuresnapshot",
            constraint=models.UniqueConstraint(
                fields=("structure_id", "captured_at"),
                name="buh_structure_snapshot_unique",
            ),
        ),
    ]
