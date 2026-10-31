import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("buh_max_history", "0007_public_provider_gaps_and_response_validators"),
    ]

    operations = [
        migrations.AlterField(
            model_name="archiveconfiguration",
            name="minimum_free_gib",
            field=models.PositiveIntegerField(
                default=25,
                help_text="Archiving pauses before the host drops below this much free space. Set to 0 to disable the reserve.",
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(2048),
                ],
            ),
        ),
    ]
