from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def preserve_existing_calculated_tax(apps, schema_editor):
    del schema_editor
    TaxAssessment = apps.get_model("buh_moon_tax", "TaxAssessment")
    TaxAssessment.objects.update(calculated_tax_due=models.F("tax_due"))


def restore_effective_tax(apps, schema_editor):
    del schema_editor
    TaxAssessment = apps.get_model("buh_moon_tax", "TaxAssessment")
    TaxAssessment.objects.update(tax_due=models.F("calculated_tax_due"))


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("buh_moon_tax", "0004_fix_post_2022_compression_units"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="moontaxpermission",
            options={
                "managed": False,
                "default_permissions": (),
                "permissions": (
                    ("view_moon_tax", "Can open the Moon Tax application"),
                    ("view_own_tax", "Can see own account and character tax records"),
                    ("view_all_tax", "Can see all linked account tax records"),
                    ("view_unlinked_miners", "Can see unlinked and outside miners"),
                    ("view_mining_quantities", "Can see mined ore types and quantities"),
                    ("view_mining_values", "Can see Jita valuations and calculated tax"),
                    ("view_payment_evidence", "Can see payment evidence and review history"),
                    ("run_tax_audit", "Can start a moon tax audit"),
                    ("review_payments", "Can approve, reject, split, or classify payments"),
                    ("manage_bill_adjustments", "Can waive, correct, void, or restore moon tax bills"),
                    ("preview_member_view", "Can open a read-only Moon Tax view as another Auth user"),
                    ("manage_tax_policy", "Can edit tax rates, periods, and compression rules"),
                    ("manage_exemptions", "Can add, change, and end tax exemptions"),
                    ("manage_enforcement", "Can configure future tax enforcement rules"),
                    ("export_tax_data", "Can export allowed Moon Tax data"),
                    ("manage_access", "Can manage Moon Tax access groups"),
                ),
            },
        ),
        migrations.AddField(
            model_name="taxconfiguration",
            name="permission_schema_version",
            field=models.CharField(
                default="0.2.2",
                editable=False,
                help_text=(
                    "Internal marker so new permissions are granted only once per release."
                ),
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="taxconfiguration",
            name="exemption_rechecked_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="taxconfiguration",
            name="exemption_state_hash",
            field=models.CharField(
                blank=True,
                default="",
                editable=False,
                help_text=(
                    "Internal fingerprint used to avoid rescanning unchanged historical "
                    "exemption data on every audit."
                ),
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="taxconfiguration",
            name="small_balance_threshold",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("50000.00"),
                help_text=(
                    "Balances at or below this amount get a one-click small-balance "
                    "waiver shortcut. Nothing is waived automatically."
                ),
                max_digits=30,
                validators=[django.core.validators.MinValueValidator(Decimal("0.00"))],
            ),
        ),
        migrations.AddField(
            model_name="taxassessment",
            name="adjustment_total",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Cached total of active director adjustments.",
                max_digits=30,
            ),
        ),
        migrations.AddField(
            model_name="taxassessment",
            name="calculated_tax_due",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text=(
                    "Tax calculated from preserved mining and the extraction's rate."
                ),
                max_digits=30,
            ),
        ),
        migrations.RunPython(
            preserve_existing_calculated_tax,
            restore_effective_tax,
        ),
        migrations.AlterField(
            model_name="taxassessment",
            name="status",
            field=models.CharField(
                choices=[
                    ("OPEN", "Open"),
                    ("DUE", "Due"),
                    ("PARTIAL", "Partially paid"),
                    ("PAID", "Paid"),
                    ("WAIVED", "Waived / voided"),
                    ("EXEMPT", "Exempt"),
                    ("REVIEW", "Needs review"),
                ],
                db_index=True,
                default="OPEN",
                max_length=12,
            ),
        ),
        migrations.CreateModel(
            name="AssessmentAdjustment",
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
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("WAIVER", "Waive balance"),
                            ("CORRECTION", "Director correction"),
                            ("VOID", "Void bill"),
                        ],
                        max_length=12,
                    ),
                ),
                (
                    "amount",
                    models.DecimalField(
                        decimal_places=2,
                        help_text="Signed ISK adjustment. Credits and waivers are negative.",
                        max_digits=30,
                    ),
                ),
                ("reason", models.CharField(max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("reversed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "reversal_reason",
                    models.CharField(blank=True, default="", max_length=500),
                ),
                (
                    "assessment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="adjustments",
                        to="buh_moon_tax.taxassessment",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="buh_moon_tax_adjustments_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "reversed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="buh_moon_tax_adjustments_reversed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("-created_at", "-pk")},
        ),
        migrations.AddIndex(
            model_name="assessmentadjustment",
            index=models.Index(
                fields=["assessment", "reversed_at"],
                name="buh_moon_ta_assessm_c7ffb9_idx",
            ),
        ),
    ]
