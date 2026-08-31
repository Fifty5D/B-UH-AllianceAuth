# Generated for B-UH Structure Operations 0.2.0.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("buh_structure_ops", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="structureopspermission",
            options={
                "default_permissions": (),
                "managed": False,
                "permissions": (
                    ("view_structure_ops", "Can view Structure Operations"),
                    ("manage_structure_ops", "Can manage Structure Operations"),
                    (
                        "admin_structure_ops",
                        "Can administer Structure Operations access and setup",
                    ),
                    ("view_schedule", "Can see the Operations Schedule tab"),
                    (
                        "view_schedule_moons",
                        "Can see moon pop timers on the schedule",
                    ),
                    ("view_schedule_fuel", "Can see fuel timers on the schedule"),
                    (
                        "view_schedule_structure_timers",
                        "Can see reinforcement and structure timers on the schedule",
                    ),
                    ("view_schedule_wars", "Can see war timers on the schedule"),
                    ("view_schedule_custom", "Can see custom schedule events"),
                    ("add_schedule_event", "Can add custom schedule events"),
                    ("change_schedule_event", "Can edit custom schedule events"),
                    ("delete_schedule_event", "Can delete custom schedule events"),
                ),
            },
        ),
        migrations.CreateModel(
            name="ScheduleEvent",
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
                ("title", models.CharField(max_length=140)),
                (
                    "description",
                    models.TextField(blank=True, default="", max_length=4000),
                ),
                ("location", models.CharField(blank=True, default="", max_length=180)),
                ("starts_at", models.DateTimeField(db_index=True)),
                ("ends_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("all_day", models.BooleanField(default=False)),
                (
                    "color",
                    models.CharField(
                        choices=[
                            ("teal", "Teal"),
                            ("blue", "Blue"),
                            ("violet", "Violet"),
                            ("gold", "Gold"),
                            ("orange", "Orange"),
                            ("red", "Red"),
                            ("slate", "Slate"),
                        ],
                        default="violet",
                        max_length=12,
                    ),
                ),
                ("link", models.URLField(blank=True, default="", max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="buh_schedule_events_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="buh_schedule_events_updated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "custom schedule event",
                "verbose_name_plural": "custom schedule events",
                "ordering": ("starts_at", "title"),
                "indexes": [
                    models.Index(
                        fields=["starts_at", "ends_at"],
                        name="buh_structu_starts__20c14f_idx",
                    )
                ],
            },
        ),
    ]
