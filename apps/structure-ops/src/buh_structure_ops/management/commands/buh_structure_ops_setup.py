"""Configure the existing Member and Director groups and import corporations."""

from allianceauth.eveonline.models import EveCharacter
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from buh_structure_ops.access_roles import (
    apply_default_access,
    permissions,
    resolve_access_configuration,
)
from buh_structure_ops.console_theme import sync_console_theme
from buh_structure_ops.services import synchronize_tracked_corporations

LEGACY_GROUP_NAMES = (
    "Structure Operations Viewers",
    "Structure Operations Managers",
    "Structure Operations Administrators",
)

# The exact permissions assigned by v0.1.0. A legacy group is removed only when
# it still contains no unrelated permissions, so a repurposed Auth group is
# never deleted by the updater.
LEGACY_PERMISSION_VALUES = (
    "buh_structure_ops.view_structure_ops",
    "buh_structure_ops.manage_structure_ops",
    "buh_structure_ops.admin_structure_ops",
    "structures.basic_access",
    "structures.view_all_structures",
    "structures.view_structure_fit",
    "structures.add_structure_owner",
    "moonmining.basic_access",
    "moonmining.extractions_access",
    "moonmining.view_all_moons",
    "moonmining.add_refinery_owner",
)


class Command(BaseCommand):
    help = "Configure Member/Director access and import connected corporations."

    def add_arguments(self, parser):
        parser.add_argument("--admin-character", default="Fifty5D")
        parser.add_argument("--member-group", default="")
        parser.add_argument("--director-group", default="")
        parser.add_argument("--dry-run", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        del args
        dry_run = options["dry_run"]
        try:
            config, member_created, director_created, reservations = (
                resolve_access_configuration(
                    member_name=options["member_group"],
                    director_name=options["director_group"],
                )
            )
            apply_default_access(config.member_group, config.director_group)
        except (Permission.DoesNotExist, ValueError) as exc:
            raise CommandError(
                f"Could not configure access groups: {exc}. Ensure migrations are current."
            ) from exc

        member_group = config.member_group
        director_group = config.director_group
        member_name = member_group.name
        director_name = director_group.name

        self.stdout.write(
            f"{'Would create' if dry_run and member_created else 'Configured'} "
            f'{member_name}: Moon Mining list + moon schedule only.'
        )
        self.stdout.write(
            f"{'Would create' if dry_run and director_created else 'Configured'} "
            f'{director_name}: full Structure Operations, schedule, and Moon Mining access.'
        )

        for label, count in reservations.items():
            if count:
                group_name = member_name if label == "member" else director_name
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Adopted the existing reserved Discord role "{group_name}"; '
                        "Alliance Auth will now manage it by that exact name."
                    )
                )

        legacy_permissions = permissions(LEGACY_PERMISSION_VALUES)
        legacy_permission_ids = {permission.pk for permission in legacy_permissions}
        for legacy_group in Group.objects.filter(name__in=LEGACY_GROUP_NAMES):
            members = list(legacy_group.user_set.all())
            member_group.user_set.add(*members)
            if legacy_group.name == "Structure Operations Administrators":
                director_group.user_set.add(*members)

            unrelated = legacy_group.permissions.exclude(pk__in=legacy_permission_ids)
            if unrelated.exists():
                legacy_group.permissions.remove(*legacy_permissions)
                self.stdout.write(
                    self.style.WARNING(
                        f'Preserved legacy group "{legacy_group.name}" because it has '
                        "unrelated permissions; only old Structure Operations permissions "
                        "were removed."
                    )
                )
                continue

            legacy_name = legacy_group.name
            member_count = len(members)
            legacy_group.delete()
            self.stdout.write(
                f'Removed legacy group "{legacy_name}" after safely migrating '
                f"{member_count} member(s)."
            )

        admin_character = options.get("admin_character")
        if admin_character:
            try:
                character = EveCharacter.objects.select_related(
                    "character_ownership__user"
                ).get(character_name__iexact=admin_character)
                user = character.character_ownership.user
            except EveCharacter.DoesNotExist as exc:
                raise CommandError(
                    f'Admin character "{admin_character}" is not known to Alliance Auth.'
                ) from exc
            except AttributeError as exc:
                raise CommandError(
                    f'Admin character "{admin_character}" has no Auth owner.'
                ) from exc
            user.groups.add(director_group)
            self.stdout.write(
                f'Confirmed {character.character_name} in "{director_name}".'
            )

        imported = synchronize_tracked_corporations()
        self.stdout.write(f"Imported {imported} newly connected corporation(s).")
        try:
            theme_changed = sync_console_theme(dry_run=dry_run)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if theme_changed:
            self.stdout.write(
                "Would apply the shared console style."
                if dry_run else "Applied the shared console style."
            )
        if dry_run:
            transaction.set_rollback(True)
            self.stdout.write(
                self.style.WARNING("Dry run complete. No changes were made.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Access is ready on "{member_name}" and "{director_name}".'
                )
            )
