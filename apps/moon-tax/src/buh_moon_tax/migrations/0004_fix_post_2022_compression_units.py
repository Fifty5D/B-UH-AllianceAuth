from django.db import migrations, models

LEGACY_AUTO_NOTE = (
    "Auto-discovered by exact EVE type name; editable by directors."
)
CURRENT_AUTO_NOTE = (
    "Auto-discovered by exact EVE type name using the post-2022 "
    "1:1 item-count conversion; editable by directors."
)


def repair_legacy_auto_discovered_rules(apps, schema_editor):
    del schema_editor
    CompressionRule = apps.get_model("buh_moon_tax", "CompressionRule")
    CompressionRule.objects.filter(
        raw_units=100,
        compressed_units=1,
        notes=LEGACY_AUTO_NOTE,
    ).update(raw_units=1, notes=CURRENT_AUTO_NOTE)


def restore_legacy_auto_discovered_rules(apps, schema_editor):
    del schema_editor
    CompressionRule = apps.get_model("buh_moon_tax", "CompressionRule")
    CompressionRule.objects.filter(
        raw_units=1,
        compressed_units=1,
        notes=CURRENT_AUTO_NOTE,
    ).update(raw_units=100, notes=LEGACY_AUTO_NOTE)


class Migration(migrations.Migration):
    dependencies = [
        ("buh_moon_tax", "0003_paymentcandidate_exempt_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="compressionrule",
            name="raw_units",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.RunPython(
            repair_legacy_auto_discovered_rules,
            restore_legacy_auto_discovered_rules,
        ),
    ]
