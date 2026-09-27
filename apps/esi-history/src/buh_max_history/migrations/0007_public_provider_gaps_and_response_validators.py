from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("buh_max_history", "0006_publicarchivefile_source_time"),
    ]

    operations = [
        migrations.AlterField(
            model_name="publiccatalogindex",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("CATALOGED", "Cataloged"),
                    ("FAILED", "Failed"),
                    ("UNAVAILABLE", "Provider index unavailable"),
                ],
                db_index=True,
                default="PENDING",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="publicarchivefile",
            name="stored_etag",
            field=models.CharField(blank=True, default="", max_length=300),
        ),
        migrations.AddField(
            model_name="publicarchivefile",
            name="stored_modified",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
    ]
