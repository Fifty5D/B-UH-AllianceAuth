"""Alliance Auth admin screens for fine-grained access and auditable decisions."""

from allianceauth.groupmanagement.models import ReservedGroupName
from django.contrib import admin, messages
from django.db import transaction
from django.db.models import Count, Prefetch, Sum
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html

from .forms import (
    MoonTaxAccessGroupForm,
    StructureTaxPolicyForm,
    TaxConfigurationForm,
    TaxExemptionForm,
    moon_tax_permissions,
)
from .models import (
    AssessmentAdjustment,
    AuditRun,
    BillingPreference,
    CompressionRule,
    EnforcementRule,
    MiningLine,
    MoonTaxAccessGroup,
    NotificationState,
    PaymentAllocation,
    PaymentCandidate,
    PaymentRecipient,
    PriceSnapshot,
    StructureTaxPolicy,
    TaxAssessment,
    TaxConfiguration,
    TaxExemption,
    TaxPeriod,
)


class PermissionAdminMixin:
    required_permission = "manage_tax_policy"

    def _allowed(self, request):
        return request.user.is_superuser or request.user.has_perm(
            f"buh_moon_tax.{self.required_permission}"
        )

    def has_module_permission(self, request):
        return self._allowed(request)

    def has_view_permission(self, request, obj=None):
        return self._allowed(request)

    def has_change_permission(self, request, obj=None):
        return self._allowed(request)

    def has_add_permission(self, request):
        return self._allowed(request)

    def has_delete_permission(self, request, obj=None):
        return self._allowed(request)


@admin.register(MoonTaxAccessGroup)
class MoonTaxAccessGroupAdmin(PermissionAdminMixin, admin.ModelAdmin):
    """Create, rename, populate, or remove app access without deleting Auth groups."""

    required_permission = "manage_access"
    form = MoonTaxAccessGroupForm
    ordering = ("name",)
    search_fields = ("name",)
    list_display = (
        "name",
        "member_count",
        "own_view",
        "all_view",
        "values_view",
        "audit_action",
        "payment_action",
        "policy_action",
        "admin_action",
        "full_group_link",
    )
    fields = (
        "name",
        "adopt_reserved_role",
        "members",
        "moon_tax_permissions",
        "permission_guide",
        "full_group_link",
    )
    readonly_fields = ("permission_guide", "full_group_link")
    save_on_top = True
    actions = ("remove_moon_tax_access",)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(moon_tax_member_count=Count("user", distinct=True))
            .prefetch_related(
                Prefetch(
                    "permissions",
                    queryset=moon_tax_permissions(),
                    to_attr="admin_moon_tax_permissions",
                )
            )
        )

    def save_model(self, request, obj, form, change):
        if form.cleaned_data.get("adopt_reserved_role"):
            ReservedGroupName.objects.filter(
                name__iexact=form.cleaned_data["name"]
            ).delete()
        super().save_model(request, obj, form, change)
        obj.user_set.set(form.cleaned_data["members"])
        available = list(moon_tax_permissions())
        selected = {
            permission.pk for permission in form.cleaned_data["moon_tax_permissions"]
        }
        obj.permissions.remove(*(item for item in available if item.pk not in selected))
        obj.permissions.add(*(item for item in available if item.pk in selected))

    @admin.action(description="Remove Moon Tax access only (preserve Auth group and Discord role)")
    def remove_moon_tax_access(self, request, queryset):
        available = list(moon_tax_permissions())
        for group in queryset:
            group.permissions.remove(*available)
        self.message_user(
            request,
            f"Removed Moon Tax permissions from {queryset.count()} group(s). "
            "The Auth groups, members, Discord roles, and unrelated permissions were preserved.",
            messages.SUCCESS,
        )

    @staticmethod
    def _codes(obj):
        prefetched = getattr(obj, "admin_moon_tax_permissions", None)
        if prefetched is not None:
            return {item.codename for item in prefetched}
        return set(
            obj.permissions.filter(
                content_type__app_label="buh_moon_tax"
            ).values_list("codename", flat=True)
        )

    @admin.display(description="Members", ordering="moon_tax_member_count")
    def member_count(self, obj):
        return getattr(obj, "moon_tax_member_count", obj.user_set.count())

    @admin.display(boolean=True, description="Own")
    def own_view(self, obj):
        return "view_own_tax" in self._codes(obj)

    @admin.display(boolean=True, description="All linked")
    def all_view(self, obj):
        return "view_all_tax" in self._codes(obj)

    @admin.display(boolean=True, description="Values")
    def values_view(self, obj):
        return "view_mining_values" in self._codes(obj)

    @admin.display(boolean=True, description="Run audit")
    def audit_action(self, obj):
        return "run_tax_audit" in self._codes(obj)

    @admin.display(boolean=True, description="Review payments")
    def payment_action(self, obj):
        return "review_payments" in self._codes(obj)

    @admin.display(boolean=True, description="Policy")
    def policy_action(self, obj):
        return "manage_tax_policy" in self._codes(obj)

    @admin.display(boolean=True, description="Access admin")
    def admin_action(self, obj):
        return "manage_access" in self._codes(obj)

    @admin.display(description="Full group editor")
    def full_group_link(self, obj):
        if not obj or not obj.pk:
            return "Save the group before opening the full editor."
        try:
            url = reverse("admin:groupmanagement_group_change", args=(obj.pk,))
        except NoReverseMatch:
            return "Alliance Auth group editor unavailable"
        return format_html('<a href="{}">Open full Alliance Auth group</a>', url)

    @admin.display(description="Permission guide")
    def permission_guide(self, obj):
        del obj
        return format_html(
            "<p><strong>Recommended presets are only starting points.</strong> Every item below can be mixed independently.</p>"
            "<ul>"
            "<li><strong>Navigation:</strong> app entry, own records, all linked accounts, and unlinked/outside miners.</li>"
            "<li><strong>Sensitive fields:</strong> ore quantities, Jita values/tax, and payment evidence.</li>"
            "<li><strong>Actions:</strong> run audits, review/split payments, edit policy, edit exemptions, configure enforcement, and export.</li>"
            "<li><strong>Access administration:</strong> create/rename groups and configure this matrix.</li>"
            "</ul>"
        )


@admin.register(TaxConfiguration)
class TaxConfigurationAdmin(PermissionAdminMixin, admin.ModelAdmin):
    form = TaxConfigurationForm
    list_display = (
        "default_tax_rate",
        "mining_window_days",
        "audit_interval_hours",
        "pricing_mode",
        "enforcement_enabled",
        "updated_at",
    )
    fieldsets = (
        (
            "Tax and billing defaults",
            {
                "fields": (
                    "default_tax_rate",
                    "mining_window_days",
                    "audit_interval_hours",
                    "combine_characters_by_default",
                    "small_balance_threshold",
                )
            },
        ),
        (
            "Jita valuation",
            {"fields": ("pricing_mode", "pricing_region_id", "pricing_location_id")},
        ),
        (
            "Default accepted payment destinations",
            {
                "fields": (
                    "default_payment_recipients",
                    "payment_character_id",
                    "payment_character_name",
                    "payment_corporation_id",
                    "payment_corporation_name",
                ),
                "description": (
                    "The checklist is inherited by Athanors without a custom list. "
                    "The legacy pair is retained as a safe fallback for existing installs."
                ),
            },
        ),
        (
            "Alliance Auth notifications",
            {
                "fields": (
                    "notify_member_new_tax",
                    "notify_member_payment_decision",
                    "notify_director_unpaid",
                    "notify_director_missing_data",
                )
            },
        ),
        (
            "Enforcement safety",
            {
                "fields": ("enforcement_enabled",),
                "description": "The installer leaves enforcement off. Individual rules also remain disabled until explicitly enabled.",
            },
        ),
        ("Change record", {"fields": ("updated_by", "updated_at")}),
    )
    readonly_fields = ("updated_by", "updated_at")

    def has_add_permission(self, request):
        return self._allowed(request) and not TaxConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(StructureTaxPolicy)
class StructureTaxPolicyAdmin(PermissionAdminMixin, admin.ModelAdmin):
    form = StructureTaxPolicyForm
    list_display = (
        "structure_name",
        "corporation_name",
        "tax_rate",
        "recipient_count",
        "effective_from",
        "effective_until",
        "enabled",
    )
    list_filter = ("enabled", "corporation_name")
    search_fields = ("structure_name", "structure_id", "corporation_name")
    filter_horizontal = ()
    fieldsets = (
        (
            "Athanor",
            {
                "fields": (
                    "structure_name",
                    "structure_id",
                    "corporation_name",
                    "corporation_id",
                )
            },
        ),
        (
            "Tax policy",
            {
                "fields": (
                    "tax_rate",
                    "effective_from",
                    "effective_until",
                    "enabled",
                    "notes",
                )
            },
        ),
        (
            "Allowed payment recipients for this Athanor",
            {
                "fields": ("payment_recipients",),
                "description": (
                    "Search and check any Auth-linked characters or represented "
                    "corporations. Leave every box clear to inherit the site default."
                ),
            },
        ),
        ("Audit record", {"fields": ("created_by", "created_at", "updated_at")}),
    )
    readonly_fields = ("created_by", "created_at", "updated_at")

    @admin.display(description="Recipients")
    def recipient_count(self, obj):
        count = obj.payment_recipients.count()
        return count if count else "Inherited"

    def get_readonly_fields(self, request, obj=None):
        base = list(self.readonly_fields)
        if obj:
            base.extend(
                (
                    "structure_name",
                    "structure_id",
                    "corporation_name",
                    "corporation_id",
                )
            )
        return tuple(base)

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(PaymentRecipient)
class PaymentRecipientAdmin(PermissionAdminMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "kind",
        "entity_id",
        "enabled",
        "discovered_from_auth",
        "used_by_athanors",
        "updated_at",
    )
    list_filter = ("kind", "enabled", "discovered_from_auth")
    search_fields = ("name", "entity_id", "notes")

    @admin.display(description="Athanor policies")
    def used_by_athanors(self, obj):
        return obj.structure_policies.count()


@admin.register(CompressionRule)
class CompressionRuleAdmin(PermissionAdminMixin, admin.ModelAdmin):
    list_display = (
        "raw_type_name",
        "compressed_type_name",
        "raw_units",
        "compressed_units",
        "enabled",
        "updated_at",
    )
    list_filter = ("enabled",)
    search_fields = (
        "raw_type_name",
        "compressed_type_name",
        "raw_type_id",
        "compressed_type_id",
    )


@admin.register(TaxExemption)
class TaxExemptionAdmin(PermissionAdminMixin, admin.ModelAdmin):
    required_permission = "manage_exemptions"
    form = TaxExemptionForm
    list_display = (
        "target",
        "effective_from",
        "effective_until",
        "active",
        "reason",
        "created_by",
    )
    list_filter = ("active", "effective_from")
    search_fields = ("auth_user__username", "character_name", "character_id", "reason")

    @admin.display(description="Account / character")
    def target(self, obj):
        return obj.auth_user or obj.character_name or obj.character_id

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(BillingPreference)
class BillingPreferenceAdmin(PermissionAdminMixin, admin.ModelAdmin):
    list_display = ("user", "combine_characters", "updated_by", "updated_at")
    search_fields = ("user__username",)

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


class PaymentAllocationInline(admin.TabularInline):
    model = PaymentAllocation
    extra = 0
    autocomplete_fields = ("assessment",)
    readonly_fields = ("created_by", "created_at")


@admin.register(PaymentCandidate)
class PaymentCandidateAdmin(PermissionAdminMixin, admin.ModelAdmin):
    required_permission = "review_payments"
    list_display = (
        "occurred_at",
        "payer_character_name",
        "source",
        "recipient_name",
        "amount",
        "allocated",
        "status",
        "reviewed_by",
    )
    list_filter = ("status", "source", "recipient_name")
    search_fields = (
        "payer_character_name",
        "auth_user__username",
        "reference",
        "source_key",
    )
    readonly_fields = (
        "source",
        "source_key",
        "source_id",
        "payer_character_id",
        "payer_character_name",
        "auth_user",
        "recipient_id",
        "recipient_name",
        "amount",
        "occurred_at",
        "reference",
        "evidence",
        "imported_by_audit",
        "created_by",
        "imported_at",
        "reviewed_by",
        "reviewed_at",
    )
    inlines = (PaymentAllocationInline,)
    actions = (
        "approve_oldest_carryover",
        "approve_oldest_overage",
        "classify_donation",
        "ignore_payment",
        "reject_payment",
        "reset_pending",
    )

    @admin.display(description="Allocated", ordering="allocated_total")
    def allocated(self, obj):
        return getattr(obj, "allocated_total", None) or 0

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(allocated_total=Sum("allocations__amount"))

    def has_add_permission(self, request):
        return self._allowed(request)

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return (
                "source_key",
                "source_id",
                "imported_by_audit",
                "imported_at",
                "reviewed_by",
                "reviewed_at",
            )
        return self.readonly_fields

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            from django.utils.timezone import now

            obj.source = PaymentCandidate.Source.MANUAL
            obj.source_key = f"manual:{request.user.pk}:{now().timestamp()}"
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        from .payments import recalculate_payment

        assessment_ids = {
            item.assessment_id
            for item in instances
            if item.assessment_id is not None
        }
        # The normal payment UI locks this same evidence row before allocating.
        # Preserve custom-split editing in admin, but make it participate in the
        # same protocol so it cannot race a one-click decision or waiver.
        with transaction.atomic():
            locked_payment = PaymentCandidate.objects.select_for_update().get(
                pk=form.instance.pk
            )
            assessment_ids.update(
                locked_payment.allocations.exclude(
                    assessment_id__isnull=True
                ).values_list("assessment_id", flat=True)
            )
            locked_assessments = list(
                TaxAssessment.objects.select_for_update()
                .filter(pk__in=assessment_ids)
                .select_related("period")
                .order_by("pk")
            )
            for deleted in formset.deleted_objects:
                deleted.delete()
            for instance in instances:
                instance.payment = locked_payment
                instance.created_by = instance.created_by or request.user
                instance.full_clean()
                instance.save()
            formset.save_m2m()
            from .payments import recalculate_assessment, recalculate_period

            recalculate_payment(locked_payment, request.user)
            # Also refresh bills whose allocations were removed entirely; they
            # no longer appear in recalculate_payment's remaining allocation set.
            for assessment in locked_assessments:
                recalculate_assessment(assessment)
            for period in {assessment.period for assessment in locked_assessments}:
                recalculate_period(period)

    def _apply(self, request, queryset, disposition):
        from .payments import decide_payment

        protected = queryset.filter(status=PaymentCandidate.Status.EXEMPT).count()
        queryset = queryset.exclude(status=PaymentCandidate.Status.EXEMPT)
        completed = 0
        for payment in queryset:
            decide_payment(payment, disposition=disposition, director=request.user)
            completed += 1
        self.message_user(request, f"Reviewed {completed} payment(s).", messages.SUCCESS)
        if protected:
            self.message_user(
                request,
                f"Skipped {protected} system-excluded payment(s). End or edit the "
                "effective exemption, then run an audit to return them to review.",
                messages.WARNING,
            )

    @admin.action(description="Approve: oldest taxes first, carry extra forward")
    def approve_oldest_carryover(self, request, queryset):
        self._apply(request, queryset, "oldest_carryover")

    @admin.action(description="Approve: oldest taxes first, accept extra as overage")
    def approve_oldest_overage(self, request, queryset):
        self._apply(request, queryset, "oldest_overage")

    @admin.action(description="Classify full amount as donation")
    def classify_donation(self, request, queryset):
        self._apply(request, queryset, "donation")

    @admin.action(description="Ignore full amount")
    def ignore_payment(self, request, queryset):
        self._apply(request, queryset, "ignore")

    @admin.action(description="Reject as not a moon-tax payment")
    def reject_payment(self, request, queryset):
        self._apply(request, queryset, "reject")

    @admin.action(description="Reset decision and return to pending")
    def reset_pending(self, request, queryset):
        self._apply(request, queryset, "pending")


@admin.register(PaymentAllocation)
class PaymentAllocationAdmin(PermissionAdminMixin, admin.ModelAdmin):
    required_permission = "review_payments"
    list_display = ("payment", "assessment", "category", "amount", "created_by", "created_at")
    list_filter = ("category",)
    search_fields = ("payment__payer_character_name", "assessment__display_name", "notes")
    autocomplete_fields = ("payment", "assessment")
    readonly_fields = tuple(field.name for field in PaymentAllocation._meta.fields)

    def has_add_permission(self, request):
        # Custom splits remain editable from the parent Payment Candidate where
        # the payment and all selected bills are locked together.
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TaxPeriod)
class TaxPeriodAdmin(PermissionAdminMixin, admin.ModelAdmin):
    list_display = (
        "moon_name",
        "structure_name",
        "pop_at",
        "closes_at",
        "tax_rate",
        "gross_value",
        "tax_due",
        "approved_paid",
        "status",
    )
    list_filter = ("status", "corporation_name")
    search_fields = ("moon_name", "structure_name", "structure_id", "corporation_name")
    readonly_fields = tuple(field.name for field in TaxPeriod._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(TaxAssessment)
class TaxAssessmentAdmin(PermissionAdminMixin, admin.ModelAdmin):
    list_display = (
        "display_name",
        "period",
        "combined_characters",
        "gross_value",
        "calculated_tax_due",
        "adjustment_total",
        "tax_due",
        "approved_paid",
        "status",
        "due_at",
    )
    list_filter = ("status", "combined_characters")
    search_fields = ("display_name", "auth_user__username", "character_id", "billing_key")
    readonly_fields = tuple(field.name for field in TaxAssessment._meta.fields)

    def has_add_permission(self, request):
        return False


@admin.register(AssessmentAdjustment)
class AssessmentAdjustmentAdmin(PermissionAdminMixin, admin.ModelAdmin):
    required_permission = "manage_bill_adjustments"
    list_display = (
        "assessment",
        "kind",
        "amount",
        "reason",
        "created_by",
        "created_at",
        "reversed_at",
    )
    list_filter = ("kind", "created_at", "reversed_at")
    search_fields = (
        "assessment__display_name",
        "assessment__billing_key",
        "reason",
        "reversal_reason",
    )
    readonly_fields = tuple(field.name for field in AssessmentAdjustment._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return self._allowed(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MiningLine)
class MiningLineAdmin(PermissionAdminMixin, admin.ModelAdmin):
    list_display = (
        "ledger_day",
        "character_name",
        "period",
        "raw_type_name",
        "raw_quantity",
        "compressed_type_name",
        "gross_value",
        "tax_due",
        "is_exempt",
        "pricing_complete",
    )
    list_filter = ("is_exempt", "pricing_complete", "ledger_day")
    search_fields = (
        "character_name",
        "character_id",
        "raw_type_name",
        "recorded_corporation_name",
    )
    readonly_fields = tuple(field.name for field in MiningLine._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AuditRun)
class AuditRunAdmin(PermissionAdminMixin, admin.ModelAdmin):
    list_display = ("id", "trigger", "status", "requested_by", "queued_at", "finished_at")
    list_filter = ("trigger", "status")
    readonly_fields = tuple(field.name for field in AuditRun._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(PriceSnapshot)
class PriceSnapshotAdmin(PermissionAdminMixin, admin.ModelAdmin):
    list_display = (
        "compressed_type_name",
        "weighted_unit_price",
        "requested_quantity",
        "priced_quantity",
        "complete",
        "fetched_at",
        "audit_run",
    )
    list_filter = ("complete",)
    search_fields = ("compressed_type_name", "compressed_type_id")
    readonly_fields = tuple(field.name for field in PriceSnapshot._meta.fields)

    def has_add_permission(self, request):
        return False


@admin.register(EnforcementRule)
class EnforcementRuleAdmin(PermissionAdminMixin, admin.ModelAdmin):
    required_permission = "manage_enforcement"
    list_display = (
        "name",
        "enabled",
        "overdue_days",
        "minimum_outstanding",
        "action",
        "target_group",
        "updated_by",
    )
    list_filter = ("enabled", "action")

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(NotificationState)
class NotificationStateAdmin(PermissionAdminMixin, admin.ModelAdmin):
    list_display = ("event_key", "last_sent_at", "payload_hash")
    readonly_fields = tuple(field.name for field in NotificationState._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
