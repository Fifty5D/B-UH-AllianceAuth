"""Admin and dashboard forms."""

from allianceauth.groupmanagement.models import ReservedGroupName
from django import forms
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.db.models import Q

from .models import (
    ScheduleEvent,
    StructureOpsAccessGroup,
    StructurePreference,
    TrackedCorporation,
)

OPS_PERMISSION_CODES = (
    "view_structure_ops",
    "manage_structure_ops",
    "admin_structure_ops",
)

SCHEDULE_PERMISSION_CODES = (
    "view_schedule",
    "view_schedule_moons",
    "view_schedule_fuel",
    "view_schedule_structure_timers",
    "view_schedule_wars",
    "view_schedule_custom",
    "add_schedule_event",
    "change_schedule_event",
    "delete_schedule_event",
)

MOON_PERMISSION_CODES = (
    "view_all_moons",
    "upload_moon_scan",
    "add_refinery_owner",
    "view_moon_ledgers",
    "view_extraction_amounts",
    "view_extraction_details",
    "view_moon_details",
    "view_moon_values",
)

MOON_NAVIGATION_CODES = ("extractions_access", "reports_access")


def ops_permissions():
    return (
        Permission.objects.filter(
            content_type__app_label="buh_structure_ops",
            codename__in=OPS_PERMISSION_CODES,
        )
        .select_related("content_type")
        .order_by("name")
    )


def schedule_permissions():
    return (
        Permission.objects.filter(
            content_type__app_label="buh_structure_ops",
            codename__in=SCHEDULE_PERMISSION_CODES,
        )
        .select_related("content_type")
        .order_by("name")
    )


def moon_permissions():
    return (
        Permission.objects.filter(
            content_type__app_label="moonmining",
            codename__in=MOON_PERMISSION_CODES,
        )
        .select_related("content_type")
        .order_by("name")
    )


def navigation_permissions():
    """Return installed app entry permissions plus Moon Mining sub-tabs."""

    return (
        Permission.objects.filter(
            Q(
                content_type__app_label__in=("structures", "moonmining"),
                codename="basic_access",
            )
            | Q(
                content_type__app_label="moonmining",
                codename__in=MOON_NAVIGATION_CODES,
            )
        )
        .select_related("content_type")
        .order_by("content_type__app_label", "name")
    )


class StructureOpsAccessGroupForm(forms.ModelForm):
    name = forms.CharField(
        max_length=150,
        label="Auth and Discord role name",
        help_text=(
            "Renaming this group changes the Discord role name Alliance Auth manages. "
            "Existing Discord roles are matched by name."
        ),
    )
    adopt_reserved_role = forms.BooleanField(
        required=False,
        label="Adopt an existing reserved Discord role",
        help_text=(
            "Check this only when the requested name already exists under Reserved "
            "Group Names. The reservation will be removed so Alliance Auth can manage "
            "that existing Discord role instead of creating a name ending in _1."
        ),
    )
    members = forms.ModelMultipleChoiceField(
        label="People with this access level",
        queryset=get_user_model().objects.none(),
        required=False,
        widget=FilteredSelectMultiple(
            verbose_name="Alliance Auth users", is_stacked=False
        ),
        help_text="Add or remove people from this Structure Operations access group.",
    )
    ops_permissions = forms.ModelMultipleChoiceField(
        label="Structure Operations permissions",
        queryset=Permission.objects.none(),
        required=False,
        widget=FilteredSelectMultiple(
            verbose_name="Structure Operations permissions", is_stacked=False
        ),
        help_text=(
            "Only Structure Operations permissions are changed here. Other Alliance "
            "Auth, Discord, and service permissions remain untouched."
        ),
    )
    navigation_permissions = forms.ModelMultipleChoiceField(
        label="Application and sub-tab visibility",
        queryset=Permission.objects.none(),
        required=False,
        widget=FilteredSelectMultiple(
            verbose_name="application and sub-tab permissions", is_stacked=False
        ),
        help_text=(
            "Controls sidebar application entries and Moon Mining's Extractions and "
            "Reports sub-tabs. Structure Operations itself is controlled above."
        ),
    )
    moon_permissions = forms.ModelMultipleChoiceField(
        label="Moon Mining data and actions",
        queryset=Permission.objects.none(),
        required=False,
        widget=FilteredSelectMultiple(
            verbose_name="Moon Mining data and action permissions", is_stacked=False
        ),
        help_text=(
            "Grant amounts, hammer details, moon details, ledgers, scans, and owner "
            "setup independently. These permissions are enforced in JSON and direct URLs."
        ),
    )
    schedule_permissions = forms.ModelMultipleChoiceField(
        label="Operations Schedule visibility and actions",
        queryset=Permission.objects.none(),
        required=False,
        widget=FilteredSelectMultiple(
            verbose_name="schedule permissions", is_stacked=False
        ),
        help_text=(
            "Choose the Schedule tab, each live timer feed, custom-event visibility, "
            "and custom-event add/edit/delete rights separately."
        ),
    )

    class Meta:
        model = StructureOpsAccessGroup
        fields = ("name",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        User = get_user_model()
        self.fields["members"].queryset = User.objects.filter(is_active=True).order_by(
            "username"
        )
        self.fields["ops_permissions"].queryset = ops_permissions()
        self.fields["navigation_permissions"].queryset = navigation_permissions()
        self.fields["moon_permissions"].queryset = moon_permissions()
        self.fields["schedule_permissions"].queryset = schedule_permissions()
        if self.instance and self.instance.pk:
            self.fields["members"].initial = self.instance.user_set.all()
            self.fields["ops_permissions"].initial = self.instance.permissions.filter(
                content_type__app_label="buh_structure_ops",
                codename__in=OPS_PERMISSION_CODES,
            )
            self.fields["navigation_permissions"].initial = (
                self.instance.permissions.filter(pk__in=navigation_permissions())
            )
            self.fields["moon_permissions"].initial = self.instance.permissions.filter(
                pk__in=moon_permissions()
            )
            self.fields["schedule_permissions"].initial = (
                self.instance.permissions.filter(pk__in=schedule_permissions())
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
                "This name is currently reserved. Check this box to let Alliance Auth "
                "manage the existing Discord role with that exact name.",
            )
        cleaned["name"] = name
        return cleaned


class ScheduleEventForm(forms.ModelForm):
    starts_at = forms.DateTimeField(
        input_formats=("%Y-%m-%dT%H:%M",),
        widget=forms.DateTimeInput(
            format="%Y-%m-%dT%H:%M",
            attrs={"class": "form-control", "type": "datetime-local"},
        ),
    )
    ends_at = forms.DateTimeField(
        required=False,
        input_formats=("%Y-%m-%dT%H:%M",),
        widget=forms.DateTimeInput(
            format="%Y-%m-%dT%H:%M",
            attrs={"class": "form-control", "type": "datetime-local"},
        ),
    )

    class Meta:
        model = ScheduleEvent
        fields = (
            "title",
            "description",
            "location",
            "starts_at",
            "ends_at",
            "all_day",
            "color",
            "link",
        )
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(
                attrs={"class": "form-control", "rows": 4}
            ),
            "location": forms.TextInput(attrs={"class": "form-control"}),
            "all_day": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "color": forms.Select(attrs={"class": "form-select"}),
            "link": forms.URLInput(attrs={"class": "form-control"}),
        }

    def clean(self):
        cleaned = super().clean()
        starts_at = cleaned.get("starts_at")
        ends_at = cleaned.get("ends_at")
        if starts_at and ends_at and ends_at < starts_at:
            raise ValidationError("The end must be after the start.")
        return cleaned


class TrackedCorporationForm(forms.ModelForm):
    class Meta:
        model = TrackedCorporation
        fields = ("corporation_id", "corporation_name", "notes")
        widgets = {
            "corporation_id": forms.NumberInput(attrs={"class": "form-control"}),
            "corporation_name": forms.TextInput(attrs={"class": "form-control"}),
            "notes": forms.TextInput(attrs={"class": "form-control"}),
        }


class StructurePreferenceForm(forms.ModelForm):
    class Meta:
        model = StructurePreference
        fields = ("target_fuel_days", "notes")
        widgets = {
            "target_fuel_days": forms.NumberInput(
                attrs={"class": "form-control", "min": 7, "max": 180}
            ),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }
