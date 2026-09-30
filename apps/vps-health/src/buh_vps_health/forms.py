from allianceauth.groupmanagement.models import ReservedGroupName
from django import forms
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission

from .access import PERMISSION_CODES
from .models import HealthConfiguration, VpsHealthAccessGroup


def health_permissions():
    return (
        Permission.objects.filter(
            content_type__app_label="buh_vps_health",
            codename__in=PERMISSION_CODES,
        )
        .select_related("content_type")
        .order_by("name")
    )


class HealthConfigurationForm(forms.ModelForm):
    class Meta:
        model = HealthConfiguration
        fields = "__all__"


class VpsHealthAccessGroupForm(forms.ModelForm):
    name = forms.CharField(
        max_length=150,
        label="Auth and Discord role name",
        help_text="Rename this existing Auth group here if your Discord role changes.",
    )
    adopt_reserved_role = forms.BooleanField(
        required=False,
        label="Adopt an existing reserved Discord role",
        help_text="Use this only when Alliance Auth has reserved this exact role name.",
    )
    members = forms.ModelMultipleChoiceField(
        queryset=get_user_model().objects.none(),
        required=False,
        widget=FilteredSelectMultiple("Alliance Auth users", is_stacked=False),
    )
    health_permissions = forms.ModelMultipleChoiceField(
        queryset=Permission.objects.none(),
        required=False,
        widget=FilteredSelectMultiple("VPS Health permissions", is_stacked=False),
        help_text="Every metric family and operational action is independently selectable.",
    )

    class Meta:
        model = VpsHealthAccessGroup
        fields = ("name",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["members"].queryset = (
            get_user_model().objects.filter(is_active=True).order_by("username")
        )
        self.fields["health_permissions"].queryset = health_permissions()
        if self.instance and self.instance.pk:
            self.fields["members"].initial = self.instance.user_set.all()
            self.fields[
                "health_permissions"
            ].initial = self.instance.permissions.filter(pk__in=health_permissions())

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
            self.add_error(
                "name", "Another Alliance Auth group already uses this name."
            )
        if ReservedGroupName.objects.filter(
            name__iexact=name
        ).exists() and not cleaned.get("adopt_reserved_role"):
            self.add_error(
                "adopt_reserved_role",
                "Check this box to adopt the reserved Discord role with this exact name.",
            )
        cleaned["name"] = name
        return cleaned
