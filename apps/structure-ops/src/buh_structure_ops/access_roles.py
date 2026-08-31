"""Resolve, configure, and safely rename the two default access groups."""

from __future__ import annotations

import re

from allianceauth.groupmanagement.models import ReservedGroupName
from django.contrib.auth.models import Group, Permission
from django.db import transaction

from .models import AccessRoleConfiguration

DEFAULT_MEMBER_GROUP = "Member"
DEFAULT_DIRECTOR_GROUP = "Director"

MEMBER_PERMISSIONS = (
    "moonmining.basic_access",
    "moonmining.extractions_access",
    "buh_structure_ops.view_schedule",
    "buh_structure_ops.view_schedule_moons",
)

DIRECTOR_PERMISSIONS = (
    "buh_structure_ops.view_structure_ops",
    "buh_structure_ops.manage_structure_ops",
    "buh_structure_ops.admin_structure_ops",
    "buh_structure_ops.view_schedule",
    "buh_structure_ops.view_schedule_moons",
    "buh_structure_ops.view_schedule_fuel",
    "buh_structure_ops.view_schedule_structure_timers",
    "buh_structure_ops.view_schedule_wars",
    "buh_structure_ops.view_schedule_custom",
    "buh_structure_ops.add_schedule_event",
    "buh_structure_ops.change_schedule_event",
    "buh_structure_ops.delete_schedule_event",
    "structures.basic_access",
    "structures.view_all_structures",
    "structures.view_structure_fit",
    "structures.add_structure_owner",
    "moonmining.basic_access",
    "moonmining.extractions_access",
    "moonmining.reports_access",
    "moonmining.view_all_moons",
    "moonmining.upload_moon_scan",
    "moonmining.add_refinery_owner",
    "moonmining.view_moon_ledgers",
    "moonmining.view_extraction_amounts",
    "moonmining.view_extraction_details",
    "moonmining.view_moon_details",
    "moonmining.view_moon_values",
)

MEMBER_DENIED_PERMISSIONS = (
    "moonmining.reports_access",
    "moonmining.view_all_moons",
    "moonmining.upload_moon_scan",
    "moonmining.add_refinery_owner",
    "moonmining.view_moon_ledgers",
    "moonmining.view_extraction_amounts",
    "moonmining.view_extraction_details",
    "moonmining.view_moon_details",
    "moonmining.view_moon_values",
    "buh_structure_ops.view_structure_ops",
    "buh_structure_ops.manage_structure_ops",
    "buh_structure_ops.admin_structure_ops",
    "buh_structure_ops.view_schedule_fuel",
    "buh_structure_ops.view_schedule_structure_timers",
    "buh_structure_ops.view_schedule_wars",
    "buh_structure_ops.view_schedule_custom",
    "buh_structure_ops.add_schedule_event",
    "buh_structure_ops.change_schedule_event",
    "buh_structure_ops.delete_schedule_event",
)


def selected_groups() -> tuple[Group, Group]:
    """Return the editable shared Member/Director role selections."""

    config = (
        AccessRoleConfiguration.objects.select_related(
            "member_group", "director_group"
        )
        .filter(singleton_id=1)
        .first()
    )
    if config and config.member_group_id and config.director_group_id:
        return config.member_group, config.director_group
    member, _ = Group.objects.get_or_create(name=DEFAULT_MEMBER_GROUP)
    director, _ = Group.objects.get_or_create(name=DEFAULT_DIRECTOR_GROUP)
    return member, director


def permission(value: str) -> Permission:
    app_label, codename = value.split(".", 1)
    return Permission.objects.get(
        content_type__app_label=app_label, codename=codename
    )


def permissions(values) -> list[Permission]:
    return [permission(value) for value in values]


def _merge_group(source: Group, target: Group) -> None:
    """Move the useful parts of an accidental alias before removing it."""

    target.user_set.add(*source.user_set.all())
    target.permissions.add(*source.permissions.all())
    try:
        source_auth = source.authgroup
        target_auth = target.authgroup
    except AttributeError:
        source_auth = target_auth = None
    if source_auth and target_auth:
        target_auth.group_leaders.add(*source_auth.group_leaders.all())
        target_auth.group_leader_groups.add(*source_auth.group_leader_groups.all())
        target_auth.states.add(*source_auth.states.all())
    source.delete()


def _alias_candidates(name: str, marker_permission: str):
    app_label, codename = marker_permission.split(".", 1)
    return (
        Group.objects.filter(name__iregex=rf"^{re.escape(name)}_[0-9]+$")
        .filter(
            permissions__content_type__app_label=app_label,
            permissions__codename=codename,
        )
        .distinct()
        .order_by("pk")
    )


def adopt_named_group(name: str, marker_permission: str) -> tuple[Group, bool, int]:
    """Adopt an existing Discord role name and repair AA's reserved-name suffix."""

    desired_name = name.strip()
    if not desired_name:
        raise ValueError("Access group names cannot be blank.")

    reservations = ReservedGroupName.objects.filter(name__iexact=desired_name)
    removed_reservations = reservations.count()
    reservations.delete()

    target = Group.objects.filter(name__iexact=desired_name).first()
    aliases = list(_alias_candidates(desired_name, marker_permission))
    created = False
    if target is None and aliases:
        target = aliases.pop(0)
        target.name = desired_name
        target.save(update_fields=("name",))
    elif target is None:
        target = Group.objects.create(name=desired_name)
        created = True

    for alias in aliases:
        if alias.pk != target.pk:
            _merge_group(alias, target)

    if target.name.casefold() != desired_name.casefold():
        raise ValueError(
            f'Alliance Auth could not adopt the exact group name "{desired_name}".'
        )
    return target, created, removed_reservations


def apply_default_access(member_group: Group, director_group: Group) -> None:
    """Add initial presets without undoing later administrator customization."""

    member_group.permissions.add(*permissions(MEMBER_PERMISSIONS))
    director_group.permissions.add(*permissions(DIRECTOR_PERMISSIONS))


def remove_managed_access(group: Group) -> None:
    """Remove only access controlled by this plugin from a former default group."""

    managed = set(DIRECTOR_PERMISSIONS) | set(MEMBER_PERMISSIONS)
    group.permissions.remove(*permissions(sorted(managed)))


@transaction.atomic
def resolve_access_configuration(
    *, member_name: str = "", director_name: str = ""
) -> tuple[AccessRoleConfiguration, bool, bool, dict[str, int]]:
    """Return the persistent selection, adopting requested role names when needed."""

    config = AccessRoleConfiguration.objects.select_related(
        "member_group", "director_group"
    ).filter(singleton_id=1).first()
    member_override = member_name.strip()
    director_override = director_name.strip()

    if (
        config
        and not member_override
        and not director_override
        and config.member_group_id
        and config.director_group_id
    ):
        return config, False, False, {"member": 0, "director": 0}

    desired_member = member_override or DEFAULT_MEMBER_GROUP
    desired_director = director_override or DEFAULT_DIRECTOR_GROUP
    if desired_member.casefold() == desired_director.casefold():
        raise ValueError("Member and Director group names must be different.")

    if config and config.member_group_id and not member_override:
        member_group = config.member_group
        member_created = False
        member_reserved = 0
    else:
        member_group, member_created, member_reserved = adopt_named_group(
            desired_member, "buh_structure_ops.view_schedule_moons"
        )

    if config and config.director_group_id and not director_override:
        director_group = config.director_group
        director_created = False
        director_reserved = 0
    else:
        director_group, director_created, director_reserved = adopt_named_group(
            desired_director, "buh_structure_ops.admin_structure_ops"
        )

    if member_group.pk == director_group.pk:
        raise ValueError("Member and Director access must use different groups.")

    config, _ = AccessRoleConfiguration.objects.update_or_create(
        singleton_id=1,
        defaults={
            "member_group": member_group,
            "director_group": director_group,
        },
    )
    return config, member_created, director_created, {
        "member": member_reserved,
        "director": director_reserved,
    }


@transaction.atomic
def replace_access_configuration(
    config: AccessRoleConfiguration,
    *, previous_member_id: int | None,
    previous_director_id: int | None,
) -> None:
    """Apply a newly selected pair and retire access on the former selections."""

    selected_ids = {config.member_group_id, config.director_group_id}
    for old_id in {previous_member_id, previous_director_id} - selected_ids - {None}:
        old_group = Group.objects.filter(pk=old_id).first()
        if old_group:
            remove_managed_access(old_group)
    apply_default_access(config.member_group, config.director_group)
