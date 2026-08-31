"""Admin forms for Mining Analytics access management."""

from allianceauth.groupmanagement.models import ReservedGroupName
from django import forms
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission

from .models import MiningAccessGroup

MINING_PERMISSION_CODES = (
    "basic_access",
    "view_corporation",
    "view_all",
    "export_data",
    "manage_access",
)


def mining_permissions():
    """Return only permissions owned by the Mining Analytics app."""

    return Permission.objects.filter(
        content_type__app_label="buh_mining_analytics",
        codename__in=MINING_PERMISSION_CODES,
    ).select_related("content_type").order_by("name")


class MiningAccessGroupForm(forms.ModelForm):
    """Edit Mining Analytics permissions without touching other group access."""

    name = forms.CharField(
        max_length=150,
        label="Auth and Discord role name",
        help_text=(
            "Renaming this group changes the Discord role name Alliance Auth manages."
        ),
    )
    adopt_reserved_role = forms.BooleanField(
        required=False,
        label="Adopt an existing reserved Discord role",
        help_text=(
            "Check this when the exact role name is reserved so Alliance Auth can "
            "adopt it without creating a name ending in _1."
        ),
    )
    members = forms.ModelMultipleChoiceField(
        queryset=get_user_model().objects.none(),
        required=False,
        widget=FilteredSelectMultiple("Alliance Auth users", is_stacked=False),
        help_text=(
            "Changing this list changes the underlying Auth group and may change its "
            "synced Discord role membership."
        ),
    )

    mining_permissions = forms.ModelMultipleChoiceField(
        label="Mining Analytics permissions",
        queryset=Permission.objects.none(),
        required=False,
        widget=FilteredSelectMultiple(
            verbose_name="Mining Analytics permissions",
            is_stacked=False,
        ),
        help_text=(
            "Only Mining Analytics permissions are shown here. Saving this page "
            "does not remove any unrelated Alliance Auth or Discord permissions."
        ),
    )

    class Meta:
        model = MiningAccessGroup
        fields = ("name",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["members"].queryset = get_user_model().objects.filter(
            is_active=True
        ).order_by("username")
        self.fields["mining_permissions"].queryset = mining_permissions()
        if self.instance and self.instance.pk:
            self.fields["members"].initial = self.instance.user_set.all()
            self.fields["mining_permissions"].initial = self.instance.permissions.filter(
                content_type__app_label="buh_mining_analytics",
                codename__in=MINING_PERMISSION_CODES,
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
                "Check this box to adopt the reserved role with this exact name.",
            )
        cleaned["name"] = name
        return cleaned
