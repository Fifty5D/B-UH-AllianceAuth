from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("buh_vps_health", "0002_alter_vpshealthpermission_options_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="metricsnapshot",
            name="filesystems",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
