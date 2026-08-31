"""Moon tax policy, immutable source snapshots, billing, and audit models."""

from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

ZERO = Decimal("0.00")


class MoonTaxPermission(models.Model):
    """Unmanaged model used only to create fine-grained application permissions."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("view_moon_tax", "Can open the Moon Tax application"),
            ("view_own_tax", "Can see own account and character tax records"),
            ("view_all_tax", "Can see all linked account tax records"),
            ("view_unlinked_miners", "Can see unlinked and outside miners"),
            ("view_mining_quantities", "Can see mined ore types and quantities"),
            ("view_mining_values", "Can see Jita valuations and calculated tax"),
            ("view_payment_evidence", "Can see payment evidence and review history"),
            ("run_tax_audit", "Can start a moon tax audit"),
            ("review_payments", "Can approve, reject, split, or classify payments"),
            (
                "manage_bill_adjustments",
                "Can waive, correct, void, or restore moon tax bills",
            ),
            (
                "preview_member_view",
                "Can open a read-only Moon Tax view as another Auth user",
            ),
            ("manage_tax_policy", "Can edit tax rates, periods, and compression rules"),
            ("manage_exemptions", "Can add, change, and end tax exemptions"),
            ("manage_enforcement", "Can configure future tax enforcement rules"),
            ("export_tax_data", "Can export allowed Moon Tax data"),
            ("manage_access", "Can manage Moon Tax access groups"),
        )


class MoonTaxAccessGroup(Group):
    """Proxy for safely editing only Moon Tax permissions on Auth groups."""

    class Meta:
        proxy = True
        default_permissions = ()
        verbose_name = "Moon Tax access group"
        verbose_name_plural = "Moon Tax access groups"


class PaymentRecipient(models.Model):
    """Character or corporation directors allow to receive moon-tax payments."""

    class Kind(models.TextChoices):
        CHARACTER = "CHARACTER", "Character"
        CORPORATION = "CORPORATION", "Corporation"

    kind = models.CharField(max_length=12, choices=Kind.choices)
    entity_id = models.PositiveBigIntegerField(db_index=True)
    name = models.CharField(max_length=255)
    enabled = models.BooleanField(default=True)
    discovered_from_auth = models.BooleanField(default=False)
    notes = models.CharField(max_length=500, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("kind", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("kind", "entity_id"),
                name="buh_moon_tax_recipient_kind_id_unique",
            )
        ]
        verbose_name = "allowed tax recipient"
        verbose_name_plural = "allowed tax recipients"

    def __str__(self):
        return f"{self.name} · {self.get_kind_display()} ({self.entity_id})"


class TaxConfiguration(models.Model):
    """Singleton containing installation-wide defaults and notification policy."""

    class PricingMode(models.TextChoices):
        JITA_BUY = "JITA_BUY", "Jita 4-4 buy-order value"

    singleton_id = models.PositiveSmallIntegerField(
        primary_key=True, default=1, editable=False
    )
    default_tax_rate = models.DecimalField(
        max_digits=7,
        decimal_places=4,
        default=Decimal("5.0000"),
        validators=[MinValueValidator(Decimal(0)), MaxValueValidator(Decimal(100))],
        help_text="Percentage used when a refinery has no effective override.",
    )
    mining_window_days = models.PositiveSmallIntegerField(
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(14)],
        help_text="A pull accepts observer-ledger mining through this many days after pop.",
    )
    audit_interval_hours = models.PositiveSmallIntegerField(
        default=4,
        validators=[MinValueValidator(1), MaxValueValidator(24)],
    )
    combine_characters_by_default = models.BooleanField(default=True)
    permission_schema_version = models.CharField(
        max_length=20,
        default="0.2.2",
        editable=False,
        help_text="Internal marker so new permissions are granted only once per release.",
    )
    exemption_state_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        editable=False,
        help_text=(
            "Internal fingerprint used to avoid rescanning unchanged historical "
            "exemption data on every audit."
        ),
    )
    exemption_rechecked_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
    )
    small_balance_threshold = models.DecimalField(
        max_digits=30,
        decimal_places=2,
        default=Decimal("50000.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text=(
            "Balances at or below this amount get a one-click small-balance waiver "
            "shortcut. Nothing is waived automatically."
        ),
    )
    pricing_mode = models.CharField(
        max_length=20, choices=PricingMode.choices, default=PricingMode.JITA_BUY
    )
    pricing_region_id = models.PositiveBigIntegerField(default=10000002)
    pricing_location_id = models.PositiveBigIntegerField(default=60003760)
    payment_character_id = models.PositiveBigIntegerField(default=0)
    payment_character_name = models.CharField(max_length=255, default="Fifty5D")
    payment_corporation_id = models.PositiveBigIntegerField(default=0)
    payment_corporation_name = models.CharField(
        max_length=255, default="Bureau of Unified Harvesting"
    )
    default_payment_recipients = models.ManyToManyField(
        PaymentRecipient,
        blank=True,
        related_name="default_for_configurations",
        help_text=(
            "Used by a refinery when its effective policy has no custom recipient "
            "checklist. Each extraction snapshots the effective list."
        ),
    )
    notify_member_new_tax = models.BooleanField(default=True)
    notify_member_payment_decision = models.BooleanField(default=True)
    notify_director_unpaid = models.BooleanField(default=True)
    notify_director_missing_data = models.BooleanField(default=True)
    enforcement_enabled = models.BooleanField(
        default=False,
        help_text="Master switch. The release installer intentionally leaves this off.",
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
        verbose_name = "Moon Tax configuration"
        verbose_name_plural = "Moon Tax configuration"

    def save(self, *args, **kwargs):
        self.singleton_id = 1
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"Moon Tax defaults ({self.default_tax_rate}%)"


class StructureTaxPolicy(models.Model):
    """Effective-dated tax override for one refinery / Athanor."""

    structure_id = models.PositiveBigIntegerField(db_index=True)
    structure_name = models.CharField(max_length=255)
    corporation_id = models.PositiveBigIntegerField(db_index=True)
    corporation_name = models.CharField(max_length=255)
    tax_rate = models.DecimalField(
        max_digits=7,
        decimal_places=4,
        validators=[MinValueValidator(Decimal(0)), MaxValueValidator(Decimal(100))],
    )
    effective_from = models.DateTimeField(db_index=True)
    effective_until = models.DateTimeField(null=True, blank=True, db_index=True)
    enabled = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default="")
    payment_recipients = models.ManyToManyField(
        PaymentRecipient,
        blank=True,
        related_name="structure_policies",
        help_text=(
            "Checked recipients are valid for this Athanor. Leave empty to inherit "
            "the installation default checklist."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("structure_name", "-effective_from")
        indexes = [models.Index(fields=("structure_id", "effective_from"))]
        constraints = [
            models.UniqueConstraint(
                fields=("structure_id", "effective_from"),
                name="buh_moon_tax_policy_effective_unique",
            )
        ]
        verbose_name = "refinery tax policy"
        verbose_name_plural = "refinery tax policies"

    def clean(self):
        if self.effective_until and self.effective_until <= self.effective_from:
            raise ValidationError("Effective until must be after effective from.")

    def __str__(self):
        return f"{self.structure_name}: {self.tax_rate}%"


class CompressionRule(models.Model):
    """Editable raw-to-compressed ore mapping used for transparent valuations."""

    raw_type_id = models.PositiveIntegerField(unique=True)
    raw_type_name = models.CharField(max_length=255)
    compressed_type_id = models.PositiveIntegerField(db_index=True)
    compressed_type_name = models.CharField(max_length=255)
    raw_units = models.PositiveIntegerField(default=1)
    compressed_units = models.PositiveIntegerField(default=1)
    enabled = models.BooleanField(default=True)
    notes = models.CharField(max_length=500, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("raw_type_name",)
        verbose_name = "ore compression rule"
        verbose_name_plural = "ore compression rules"

    def clean(self):
        if not self.raw_units or not self.compressed_units:
            raise ValidationError("Compression quantities must be greater than zero.")

    @property
    def multiplier(self):
        return Decimal(self.compressed_units) / Decimal(self.raw_units)

    def __str__(self):
        return f"{self.raw_type_name} → {self.compressed_type_name}"


class AuditRun(models.Model):
    """One manual or scheduled end-to-end source refresh and reconciliation."""

    class Trigger(models.TextChoices):
        MANUAL = "MANUAL", "Manual button"
        SCHEDULED = "SCHEDULED", "Scheduled"
        INSTALL = "INSTALL", "Installation / backfill"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        REFRESHING = "REFRESHING", "Refreshing ESI sources"
        RUNNING = "RUNNING", "Reconciling"
        COMPLETE = "COMPLETE", "Complete"
        WARNING = "WARNING", "Complete with warnings"
        FAILED = "FAILED", "Failed"

    trigger = models.CharField(max_length=12, choices=Trigger.choices)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="buh_moon_tax_audits_requested",
    )
    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    source_refresh_requested_at = models.DateTimeField(null=True, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ("-queued_at",)

    def __str__(self):
        return f"Audit #{self.pk} · {self.get_status_display()}"


class PriceSnapshot(models.Model):
    """Order-book evidence retained with each audit instead of mutable live prices."""

    audit_run = models.ForeignKey(
        AuditRun, on_delete=models.CASCADE, related_name="price_snapshots"
    )
    compressed_type_id = models.PositiveIntegerField(db_index=True)
    compressed_type_name = models.CharField(max_length=255)
    requested_quantity = models.DecimalField(max_digits=30, decimal_places=8)
    priced_quantity = models.DecimalField(max_digits=30, decimal_places=8)
    weighted_unit_price = models.DecimalField(
        max_digits=30, decimal_places=8, default=ZERO
    )
    total_value = models.DecimalField(max_digits=30, decimal_places=2, default=ZERO)
    region_id = models.PositiveBigIntegerField(default=10000002)
    location_id = models.PositiveBigIntegerField(default=60003760)
    source = models.CharField(max_length=80, default="ESI Jita buy orders")
    complete = models.BooleanField(default=False)
    fetched_at = models.DateTimeField(auto_now_add=True)
    raw_evidence = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("compressed_type_name",)
        constraints = [
            models.UniqueConstraint(
                fields=("audit_run", "compressed_type_id"),
                name="buh_moon_tax_price_run_type_unique",
            )
        ]

    def __str__(self):
        return f"{self.compressed_type_name} @ {self.weighted_unit_price}"


class TaxPeriod(models.Model):
    """One billable period tied to one moon extraction."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Collecting mining"
        DUE = "DUE", "Closed and due"
        SETTLED = "SETTLED", "Settled"
        CANCELED = "CANCELED", "Canceled"
        NEEDS_REVIEW = "REVIEW", "Needs data review"

    source_started_at = models.DateTimeField()
    structure_id = models.PositiveBigIntegerField(db_index=True)
    structure_name = models.CharField(max_length=255)
    corporation_id = models.PositiveBigIntegerField(db_index=True)
    corporation_name = models.CharField(max_length=255)
    moon_id = models.PositiveBigIntegerField(null=True, blank=True)
    moon_name = models.CharField(max_length=255, blank=True, default="")
    extraction_started_at = models.DateTimeField()
    pop_at = models.DateTimeField(db_index=True)
    closes_at = models.DateTimeField(db_index=True)
    due_at = models.DateTimeField(db_index=True)
    tax_rate = models.DecimalField(max_digits=7, decimal_places=4)
    allowed_recipients = models.JSONField(
        default=list,
        blank=True,
        help_text="Immutable character/corporation payment recipients for this pull.",
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    gross_value = models.DecimalField(max_digits=30, decimal_places=2, default=ZERO)
    tax_due = models.DecimalField(max_digits=30, decimal_places=2, default=ZERO)
    approved_paid = models.DecimalField(
        max_digits=30, decimal_places=2, default=ZERO
    )
    last_audit_run = models.ForeignKey(
        AuditRun,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="periods",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-pop_at", "structure_name")
        constraints = [
            models.UniqueConstraint(
                fields=("structure_id", "source_started_at"),
                name="buh_moon_tax_period_source_unique",
            )
        ]
        indexes = [models.Index(fields=("status", "due_at"))]

    @property
    def outstanding(self):
        return max(ZERO, self.tax_due - self.approved_paid)

    def __str__(self):
        return f"{self.moon_name or self.structure_name} · {self.pop_at:%Y-%m-%d}"


class MiningLine(models.Model):
    """Preserved observer-ledger row valued for one extraction period."""

    period = models.ForeignKey(
        TaxPeriod, on_delete=models.CASCADE, related_name="mining_lines"
    )
    ledger_day = models.DateField(db_index=True)
    character_id = models.PositiveBigIntegerField(db_index=True)
    character_name = models.CharField(max_length=255)
    recorded_corporation_id = models.PositiveBigIntegerField(db_index=True)
    recorded_corporation_name = models.CharField(max_length=255)
    auth_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="buh_moon_tax_mining",
    )
    raw_type_id = models.PositiveIntegerField()
    raw_type_name = models.CharField(max_length=255)
    compressed_type_id = models.PositiveIntegerField(null=True, blank=True)
    compressed_type_name = models.CharField(max_length=255, blank=True, default="")
    raw_quantity = models.PositiveBigIntegerField()
    compressed_quantity = models.DecimalField(
        max_digits=30, decimal_places=8, default=ZERO
    )
    compressed_unit_price = models.DecimalField(
        max_digits=30, decimal_places=8, default=ZERO
    )
    gross_value = models.DecimalField(max_digits=30, decimal_places=2, default=ZERO)
    tax_rate = models.DecimalField(max_digits=7, decimal_places=4, default=ZERO)
    tax_due = models.DecimalField(max_digits=30, decimal_places=2, default=ZERO)
    is_exempt = models.BooleanField(default=False)
    exemption_reason = models.CharField(max_length=500, blank=True, default="")
    pricing_complete = models.BooleanField(default=False)
    last_audit_run = models.ForeignKey(
        AuditRun, on_delete=models.PROTECT, related_name="mining_lines"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("ledger_day", "character_name", "raw_type_name")
        constraints = [
            models.UniqueConstraint(
                fields=("period", "ledger_day", "character_id", "raw_type_id"),
                name="buh_moon_tax_mining_line_unique",
            )
        ]
        indexes = [
            models.Index(fields=("auth_user", "ledger_day")),
            models.Index(fields=("character_id", "ledger_day")),
        ]

    def __str__(self):
        return f"{self.character_name}: {self.raw_quantity} {self.raw_type_name}"


class BillingPreference(models.Model):
    """Optional per-account override for combined versus per-character billing."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="buh_moon_tax_billing_preference",
    )
    combine_characters = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user}: {'combined' if self.combine_characters else 'separate'}"


class TaxAssessment(models.Model):
    """Bill for an Auth account or an individual/unlinked character in one period."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        DUE = "DUE", "Due"
        PARTIAL = "PARTIAL", "Partially paid"
        PAID = "PAID", "Paid"
        WAIVED = "WAIVED", "Waived / voided"
        EXEMPT = "EXEMPT", "Exempt"
        REVIEW = "REVIEW", "Needs review"

    period = models.ForeignKey(
        TaxPeriod, on_delete=models.CASCADE, related_name="assessments"
    )
    billing_key = models.CharField(max_length=80)
    auth_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="buh_moon_tax_assessments",
    )
    character_id = models.PositiveBigIntegerField(null=True, blank=True)
    display_name = models.CharField(max_length=255)
    combined_characters = models.BooleanField(default=True)
    gross_value = models.DecimalField(max_digits=30, decimal_places=2, default=ZERO)
    calculated_tax_due = models.DecimalField(
        max_digits=30,
        decimal_places=2,
        default=ZERO,
        help_text="Tax calculated from preserved mining and the extraction's rate.",
    )
    adjustment_total = models.DecimalField(
        max_digits=30,
        decimal_places=2,
        default=ZERO,
        help_text="Cached total of active director adjustments.",
    )
    tax_due = models.DecimalField(max_digits=30, decimal_places=2, default=ZERO)
    approved_paid = models.DecimalField(
        max_digits=30, decimal_places=2, default=ZERO
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    due_at = models.DateTimeField()
    first_notified_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("period__pop_at", "display_name")
        constraints = [
            models.UniqueConstraint(
                fields=("period", "billing_key"),
                name="buh_moon_tax_assessment_key_unique",
            )
        ]
        indexes = [models.Index(fields=("status", "due_at"))]

    @property
    def outstanding(self):
        return max(ZERO, self.tax_due - self.approved_paid)

    def __str__(self):
        return f"{self.display_name} · {self.period}"


class AssessmentAdjustment(models.Model):
    """Reversible director adjustment that preserves the original calculated bill."""

    class Kind(models.TextChoices):
        WAIVER = "WAIVER", "Waive balance"
        CORRECTION = "CORRECTION", "Director correction"
        VOID = "VOID", "Void bill"

    assessment = models.ForeignKey(
        TaxAssessment,
        on_delete=models.PROTECT,
        related_name="adjustments",
    )
    kind = models.CharField(max_length=12, choices=Kind.choices)
    amount = models.DecimalField(
        max_digits=30,
        decimal_places=2,
        help_text="Signed ISK adjustment. Credits and waivers are negative.",
    )
    reason = models.CharField(max_length=500)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="buh_moon_tax_adjustments_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="buh_moon_tax_adjustments_reversed",
    )
    reversal_reason = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ("-created_at", "-pk")
        indexes = [models.Index(fields=("assessment", "reversed_at"))]

    @property
    def active(self):
        return self.reversed_at is None

    def clean(self):
        if self.amount == ZERO:
            raise ValidationError("An adjustment cannot be zero ISK.")
        if self.kind in {self.Kind.WAIVER, self.Kind.VOID} and self.amount > ZERO:
            raise ValidationError("Waivers and voids must reduce the bill.")
        if self.reversed_at and not self.reversal_reason:
            raise ValidationError("A reversal reason is required.")

    def __str__(self):
        return f"{self.assessment}: {self.amount:,.2f} ISK"


class TaxExemption(models.Model):
    """Effective-dated account or character exemption chosen by a director."""

    auth_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="buh_moon_tax_exemptions",
    )
    character_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    character_name = models.CharField(max_length=255, blank=True, default="")
    effective_from = models.DateField(db_index=True)
    effective_until = models.DateField(null=True, blank=True, db_index=True)
    reason = models.CharField(max_length=500)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="buh_moon_tax_exemptions_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-effective_from", "character_name")
        constraints = [
            models.CheckConstraint(
                condition=(
                    (Q(auth_user__isnull=False) & Q(character_id__isnull=True))
                    | (Q(auth_user__isnull=True) & Q(character_id__isnull=False))
                ),
                name="buh_moon_tax_exemption_exact_target",
            )
        ]

    def clean(self):
        if bool(self.auth_user_id) == bool(self.character_id):
            raise ValidationError("Choose exactly one Auth account or one character.")
        if self.effective_until and self.effective_until < self.effective_from:
            raise ValidationError("Effective until cannot be before effective from.")

    def __str__(self):
        target = self.auth_user or self.character_name or self.character_id
        return f"{target} from {self.effective_from}"


class PaymentCandidate(models.Model):
    """Potential payment that never counts until a director makes a decision."""

    class Source(models.TextChoices):
        WALLET = "WALLET", "Direct wallet payment"
        CONTRACT = "CONTRACT", "Completed ISK contract"
        MANUAL = "MANUAL", "Director-entered manual credit"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending review"
        APPROVED = "APPROVED", "Approved and allocated"
        PARTIAL = "PARTIAL", "Partially allocated"
        REJECTED = "REJECTED", "Rejected"
        DONATION = "DONATION", "Donation"
        OVERAGE = "OVERAGE", "Accepted overage / carry-forward"
        IGNORED = "IGNORED", "Ignored"
        EXEMPT = "EXEMPT", "Excluded by exemption"

    source = models.CharField(max_length=12, choices=Source.choices)
    source_key = models.CharField(max_length=180, unique=True)
    source_id = models.PositiveBigIntegerField(null=True, blank=True)
    payer_character_id = models.PositiveBigIntegerField(null=True, blank=True)
    payer_character_name = models.CharField(max_length=255, blank=True, default="")
    auth_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="buh_moon_tax_payments",
    )
    recipient_id = models.PositiveBigIntegerField()
    recipient_name = models.CharField(max_length=255)
    amount = models.DecimalField(
        max_digits=30, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    occurred_at = models.DateTimeField(db_index=True)
    reference = models.CharField(max_length=255, blank=True, default="")
    evidence = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    imported_by_audit = models.ForeignKey(
        AuditRun,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payment_candidates",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="buh_moon_tax_manual_payments",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="buh_moon_tax_payments_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default="")
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-occurred_at", "-pk")
        indexes = [models.Index(fields=("auth_user", "status", "occurred_at"))]

    @property
    def allocated_amount(self):
        return sum((item.amount for item in self.allocations.all()), ZERO)

    @property
    def unallocated_amount(self):
        return max(ZERO, self.amount - self.allocated_amount)

    def __str__(self):
        payer = self.payer_character_name or self.auth_user or "Unknown payer"
        return f"{payer}: {self.amount:,.2f} ISK"


class PaymentAllocation(models.Model):
    """Director-approved disposition of some or all of a payment candidate."""

    class Category(models.TextChoices):
        TAX = "TAX", "Apply to selected extraction tax"
        CARRYOVER = "CARRYOVER", "Carry forward to future tax"
        OVERAGE = "OVERAGE", "Accept as overage"
        DONATION = "DONATION", "Donation"
        IGNORED = "IGNORED", "Ignore amount"

    payment = models.ForeignKey(
        PaymentCandidate, on_delete=models.CASCADE, related_name="allocations"
    )
    assessment = models.ForeignKey(
        TaxAssessment,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payment_allocations",
    )
    category = models.CharField(max_length=12, choices=Category.choices)
    amount = models.DecimalField(
        max_digits=30, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    notes = models.CharField(max_length=500, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="buh_moon_tax_allocations_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("payment", "created_at")

    def clean(self):
        if self.category == self.Category.TAX and not self.assessment_id:
            raise ValidationError("Tax allocations require a selected extraction bill.")
        if self.category != self.Category.TAX and self.assessment_id:
            raise ValidationError("Only tax allocations may select an extraction bill.")
        existing = self.payment.allocations.exclude(pk=self.pk).aggregate(
            total=models.Sum("amount")
        )["total"] or ZERO
        if existing + self.amount > self.payment.amount:
            raise ValidationError("Allocations cannot exceed the payment amount.")
        if (
            self.category == self.Category.TAX
            and self.assessment_id
            and self.payment.source != PaymentCandidate.Source.MANUAL
        ):
            allowed_ids = {
                int(item["id"])
                for item in (self.assessment.period.allowed_recipients or [])
                if item.get("id")
            }
            if allowed_ids and self.payment.recipient_id not in allowed_ids:
                raise ValidationError(
                    "This payment destination was not allowed for the selected "
                    "Athanor extraction. Choose a matching period or classify the "
                    "payment as another category."
                )

    def __str__(self):
        return f"{self.payment_id}: {self.amount} ({self.get_category_display()})"


class EnforcementRule(models.Model):
    """Disabled-by-default framework for future director-configured enforcement."""

    class Action(models.TextChoices):
        NOTIFY_ONLY = "NOTIFY", "Notify only"
        REMOVE_GROUP = "REMOVE_GROUP", "Remove an Auth group"
        ADD_GROUP = "ADD_GROUP", "Add an Auth group"

    name = models.CharField(max_length=120, unique=True)
    enabled = models.BooleanField(default=False)
    overdue_days = models.PositiveSmallIntegerField(default=7)
    minimum_outstanding = models.DecimalField(
        max_digits=30, decimal_places=2, default=Decimal("1.00")
    )
    action = models.CharField(
        max_length=20, choices=Action.choices, default=Action.NOTIFY_ONLY
    )
    target_group = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    notes = models.TextField(blank=True, default="")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if (
            self.action in {self.Action.ADD_GROUP, self.Action.REMOVE_GROUP}
            and not self.target_group_id
        ):
            raise ValidationError("This action requires an Auth group.")

    def __str__(self):
        return self.name


class NotificationState(models.Model):
    """Deduplication record for recurring Auth notifications."""

    event_key = models.CharField(max_length=255, unique=True)
    last_sent_at = models.DateTimeField()
    payload_hash = models.CharField(max_length=64)

    def __str__(self):
        return self.event_key
