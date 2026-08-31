"""Safe access, policy, and payment-review forms."""

from allianceauth.groupmanagement.models import ReservedGroupName
from django import forms
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ObjectDoesNotExist
from django.db import models as django_models

from .models import (
    MoonTaxAccessGroup,
    PaymentCandidate,
    PaymentRecipient,
    StructureTaxPolicy,
    TaxConfiguration,
    TaxExemption,
)

MOON_TAX_PERMISSION_CODES = (
    "view_moon_tax",
    "view_own_tax",
    "view_all_tax",
    "view_unlinked_miners",
    "view_mining_quantities",
    "view_mining_values",
    "view_payment_evidence",
    "run_tax_audit",
    "review_payments",
    "manage_bill_adjustments",
    "preview_member_view",
    "manage_tax_policy",
    "manage_exemptions",
    "manage_enforcement",
    "export_tax_data",
    "manage_access",
)


def moon_tax_permissions():
    return (
        Permission.objects.filter(
            content_type__app_label="buh_moon_tax",
            codename__in=MOON_TAX_PERMISSION_CODES,
        )
        .select_related("content_type")
        .order_by("name")
    )


class RecipientChecklistWidget(forms.CheckboxSelectMultiple):
    """Searchable checkbox grid that remains usable with large Auth rosters."""

    template_name = "buh_moon_tax/widgets/recipient_checklist.html"

    class Media:
        css = {"all": ("buh_moon_tax/css/admin-recipients.css",)}
        js = ("buh_moon_tax/js/admin-recipients.js",)


class SearchableAccountSelect(forms.Select):
    """Self-contained Auth-account search without requiring a User ModelAdmin."""

    class Media:
        css = {"all": ("buh_moon_tax/css/admin-account-select.css",)}
        js = ("buh_moon_tax/js/admin-account-select.js",)


class AuthUserChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        try:
            main_character = obj.profile.main_character
        except (AttributeError, ObjectDoesNotExist):
            main_character = None
        if main_character:
            return f"{main_character.character_name} — Auth account: {obj.username}"
        return f"Auth account: {obj.username}"


class MoonTaxAccessGroupForm(forms.ModelForm):
    name = forms.CharField(
        max_length=150,
        label="Auth and Discord role name",
        help_text=(
            "This is an existing Alliance Auth group and, when Discord sync is enabled, "
            "its Discord role name. It can be renamed here."
        ),
    )
    adopt_reserved_role = forms.BooleanField(
        required=False,
        label="Adopt an existing reserved Discord role",
        help_text=(
            "Use this when Alliance Auth has reserved the exact role name. The reservation "
            "is removed so the existing role can be managed without a _1 suffix."
        ),
    )
    members = forms.ModelMultipleChoiceField(
        queryset=get_user_model().objects.none(),
        required=False,
        widget=FilteredSelectMultiple("Alliance Auth users", is_stacked=False),
        help_text=(
            "Changing this list changes membership of the underlying Auth group and may "
            "therefore add or remove its synced Discord role."
        ),
    )
    moon_tax_permissions = forms.ModelMultipleChoiceField(
        queryset=Permission.objects.none(),
        required=False,
        widget=FilteredSelectMultiple("Moon Tax permissions", is_stacked=False),
        help_text=(
            "Every tab, sensitive value, workflow, export, and configuration area is "
            "separate. Unrelated Auth and Discord permissions are never changed."
        ),
    )

    class Meta:
        model = MoonTaxAccessGroup
        fields = ("name",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["members"].queryset = get_user_model().objects.filter(
            is_active=True
        ).order_by("username")
        self.fields["moon_tax_permissions"].queryset = moon_tax_permissions()
        if self.instance and self.instance.pk:
            self.fields["members"].initial = self.instance.user_set.all()
            self.fields["moon_tax_permissions"].initial = self.instance.permissions.filter(
                pk__in=moon_tax_permissions()
            )

    def clean(self):
        cleaned = super().clean()
        name = (cleaned.get("name") or "").strip()
        if not name:
            self.add_error("name", "The role name cannot be blank.")
            return cleaned
        duplicate = Group.objects.filter(name__iexact=name)
        if self.instance and self.instance.pk:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            self.add_error("name", "Another Alliance Auth group already uses this name.")
        if (
            ReservedGroupName.objects.filter(name__iexact=name).exists()
            and not cleaned.get("adopt_reserved_role")
        ):
            self.add_error(
                "adopt_reserved_role",
                "Check this box to adopt the reserved Discord role with this exact name.",
            )
        cleaned["name"] = name
        return cleaned


class TaxExemptionForm(forms.ModelForm):
    auth_user = AuthUserChoiceField(
        queryset=get_user_model().objects.none(),
        required=False,
        empty_label="Select an Auth account (optional)",
        widget=SearchableAccountSelect(
            attrs={
                "data-buh-account-select": "",
                "data-search-placeholder": "Search main character or Auth username…",
            }
        ),
    )

    class Meta:
        model = TaxExemption
        fields = (
            "auth_user",
            "character_id",
            "character_name",
            "effective_from",
            "effective_until",
            "reason",
            "active",
        )
        widgets = {
            "effective_from": forms.DateInput(attrs={"type": "date"}),
            "effective_until": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        active_or_selected = django_models.Q(is_active=True)
        if self.instance and self.instance.auth_user_id:
            active_or_selected |= django_models.Q(pk=self.instance.auth_user_id)
        self.fields["auth_user"].queryset = (
            get_user_model()
            .objects.filter(active_or_selected)
            .select_related("profile__main_character")
            .order_by("username")
        )
        self.fields["auth_user"].help_text = (
            "Exempts this Auth account and every character currently linked to it. "
            "Each audit rechecks ownership and the effective dates."
        )
        self.fields["character_id"].help_text = (
            "Use this only to exempt one specific character instead of the full Auth account."
        )


class TaxConfigurationForm(forms.ModelForm):
    default_payment_recipients = forms.ModelMultipleChoiceField(
        queryset=PaymentRecipient.objects.none(),
        required=False,
        widget=RecipientChecklistWidget,
        help_text=(
            "Default checklist for Athanors without a custom list. Run setup again "
            "at any time to discover newly linked Auth characters and corporations."
        ),
    )

    class Meta:
        model = TaxConfiguration
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_payment_recipients"].queryset = (
            PaymentRecipient.objects.filter(enabled=True).order_by("kind", "name")
        )


class PolicyDefaultsForm(forms.ModelForm):
    """Compact front-end form for the defaults directors change most often."""

    default_payment_recipients = forms.ModelMultipleChoiceField(
        queryset=PaymentRecipient.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Default payment recipients",
    )

    class Meta:
        model = TaxConfiguration
        fields = (
            "default_tax_rate",
            "small_balance_threshold",
            "default_payment_recipients",
        )
        widgets = {
            "default_tax_rate": forms.NumberInput(
                attrs={"min": "0", "max": "100", "step": "0.01"}
            ),
            "small_balance_threshold": forms.NumberInput(
                attrs={"min": "0", "step": "1000"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_payment_recipients"].queryset = (
            PaymentRecipient.objects.filter(enabled=True).order_by("kind", "name")
        )


class StructurePolicyWorkspaceForm(forms.Form):
    """Create an effective-dated per-Athanor override from the policy workspace."""

    structure_id = forms.IntegerField(widget=forms.HiddenInput)
    tax_rate = forms.DecimalField(
        min_value=0,
        max_value=100,
        max_digits=7,
        decimal_places=4,
        widget=forms.NumberInput(attrs={"min": "0", "max": "100", "step": "0.01"}),
    )
    inherit_recipients = forms.BooleanField(required=False)
    payment_recipients = forms.ModelMultipleChoiceField(
        queryset=PaymentRecipient.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    notes = forms.CharField(required=False, max_length=2000)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["payment_recipients"].queryset = (
            PaymentRecipient.objects.filter(enabled=True).order_by("kind", "name")
        )

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("inherit_recipients") and not cleaned.get(
            "payment_recipients"
        ):
            self.add_error(
                "payment_recipients",
                "Select at least one recipient or keep site-default recipients enabled.",
            )
        return cleaned


class BillAdjustmentForm(forms.Form):
    class Action(django_models.TextChoices):
        WAIVE_BALANCE = "WAIVE_BALANCE", "Waive remaining balance"
        VOID_BILL = "VOID_BILL", "Void the full bill"
        CREDIT = "CREDIT", "Apply a director credit"
        DEBIT = "DEBIT", "Add a director debit"

    action = forms.ChoiceField(choices=Action.choices)
    amount = forms.DecimalField(
        required=False,
        min_value=0.01,
        max_digits=30,
        decimal_places=2,
    )
    reason = forms.CharField(max_length=500)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("action") in {self.Action.CREDIT, self.Action.DEBIT} and not cleaned.get(
            "amount"
        ):
            self.add_error("amount", "Enter an ISK amount for a credit or debit.")
        return cleaned


class BillingPreferenceForm(forms.Form):
    """Director-facing per-account billing mode using button-style choices."""

    class Mode(django_models.TextChoices):
        DEFAULT = "DEFAULT", "Use the site default"
        COMBINED = "COMBINED", "Combine every linked character"
        SEPARATE = "SEPARATE", "Create a separate bill per character"

    mode = forms.ChoiceField(
        choices=Mode.choices,
        widget=forms.RadioSelect,
    )


class AdjustmentReversalForm(forms.Form):
    reason = forms.CharField(max_length=500)


class StructureTaxPolicyForm(forms.ModelForm):
    payment_recipients = forms.ModelMultipleChoiceField(
        queryset=PaymentRecipient.objects.none(),
        required=False,
        widget=RecipientChecklistWidget,
        label="Allowed payment recipients for this Athanor",
        help_text=(
            "Select any combination of linked characters and corporations. Empty "
            "means inherit the default checklist."
        ),
    )

    class Meta:
        model = StructureTaxPolicy
        fields = "__all__"
        widgets = {
            "effective_from": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "effective_until": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["payment_recipients"].queryset = (
            PaymentRecipient.objects.filter(enabled=True).order_by("kind", "name")
        )


class PaymentDecisionForm(forms.Form):
    class Decision(django_models.TextChoices):
        OLDEST_CARRY = "OLDEST_CARRY", "Apply oldest first; carry extra forward"
        OLDEST_OVERAGE = "OLDEST_OVERAGE", "Apply oldest first; accept extra as overage"
        DONATION = "DONATION", "Classify the full amount as a donation"
        IGNORE = "IGNORE", "Ignore the full amount"
        REJECT = "REJECT", "Reject as not a tax payment"
        RESET_PENDING = "RESET_PENDING", "Return to pending review"

    payment_id = forms.ModelChoiceField(
        queryset=PaymentCandidate.objects.none(),
        widget=forms.HiddenInput,
    )
    decision = forms.ChoiceField(choices=Decision.choices)
    note = forms.CharField(required=False, max_length=2000, widget=forms.Textarea)

    def __init__(self, *args, payment_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["payment_id"].queryset = (
            payment_queryset
            if payment_queryset is not None
            else PaymentCandidate.objects.none()
        )


class BulkPaymentDecisionForm(forms.Form):
    """Validate a bounded, permission-filtered batch of payment decisions."""

    payment_ids = forms.ModelMultipleChoiceField(
        queryset=PaymentCandidate.objects.none()
    )
    decision = forms.ChoiceField(choices=PaymentDecisionForm.Decision.choices)
    note = forms.CharField(required=False, max_length=2000)

    def __init__(self, *args, payment_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["payment_ids"].queryset = (
            payment_queryset
            if payment_queryset is not None
            else PaymentCandidate.objects.none()
        )

    def clean_payment_ids(self):
        payments = self.cleaned_data["payment_ids"]
        if payments.count() > 250:
            raise forms.ValidationError(
                "Bulk actions are limited to 250 payments at a time."
            )
        return payments


class PaymentFilterForm(forms.Form):
    SORT_CHOICES = (
        ("oldest", "Oldest first"),
        ("newest", "Newest first"),
        ("highest", "Highest amount"),
        ("lowest", "Lowest amount"),
        ("member", "Member name"),
    )
    PAGE_SIZE_CHOICES = (
        ("25", "25"),
        ("50", "50"),
        ("100", "100"),
        ("250", "250"),
    )

    q = forms.CharField(
        required=False,
        max_length=150,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Search member, character, recipient, or reference…",
                "autocomplete": "off",
                "data-payment-search": "",
            }
        ),
    )
    source = forms.ChoiceField(
        required=False,
        choices=(("", "All sources"), *PaymentCandidate.Source.choices),
    )
    recipient = forms.ChoiceField(required=False, choices=(("", "All recipients"),))
    min_amount = forms.DecimalField(required=False, min_value=0, decimal_places=2)
    max_amount = forms.DecimalField(required=False, min_value=0, decimal_places=2)
    date_from = forms.DateField(required=False)
    date_to = forms.DateField(required=False)
    sort = forms.ChoiceField(required=False, choices=SORT_CHOICES, initial="oldest")
    per_page = forms.ChoiceField(
        required=False, choices=PAGE_SIZE_CHOICES, initial="50"
    )

    def __init__(self, *args, recipient_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recipient"].choices = (
            ("", "All recipients"),
            *tuple((str(value), label) for value, label in recipient_choices),
        )
        for name in ("source", "recipient", "sort", "per_page"):
            self.fields[name].widget.attrs["class"] = "form-select"
        for name in ("min_amount", "max_amount"):
            self.fields[name].widget.attrs.update(
                {"class": "form-control", "inputmode": "decimal"}
            )
        for name in ("date_from", "date_to"):
            self.fields[name].widget = forms.DateInput(
                attrs={"class": "form-control", "type": "date"}
            )

    def clean(self):
        cleaned = super().clean()
        minimum = cleaned.get("min_amount")
        maximum = cleaned.get("max_amount")
        if minimum is not None and maximum is not None and minimum > maximum:
            self.add_error("max_amount", "Maximum must be at least the minimum.")
        start = cleaned.get("date_from")
        end = cleaned.get("date_to")
        if start and end and start > end:
            self.add_error("date_to", "End date must be on or after the start date.")
        return cleaned
