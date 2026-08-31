"""Create the content type and permissions for Mining Analytics."""

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="MiningAnalyticsPermission",
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
            ],
            options={
                "managed": False,
                "default_permissions": (),
                "permissions": (
                    (
                        "basic_access",
                        "Can access Mining Analytics and view own characters",
                    ),
                    (
                        "view_corporation",
                        "Can view Mining Analytics for members of the same corporation",
                    ),
                    (
                        "view_all",
                        "Can view Mining Analytics for all registered characters",
                    ),
                    (
                        "export_data",
                        "Can export Mining Analytics data within allowed scope",
                    ),
                ),
            },
        ),
    ]
