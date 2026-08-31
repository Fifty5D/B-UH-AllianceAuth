"""Assign initial Mining Analytics permissions to Auth groups."""

from allianceauth.eveonline.models import EveCharacter
from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Grant Mining Analytics permissions to member/admin groups safely."

    def add_arguments(self, parser):
        parser.add_argument(
            "--member-group",
            help="Existing Auth group that receives own-character access and CSV export.",
        )
        parser.add_argument(
            "--admin-group",
            default="",
            help=(
                "Existing group that receives full analytics permissions. Defaults "
                "to the shared Director role selected in Structure Operations."
            ),
        )
        parser.add_argument(
            "--admin-character",
            help="Optional Auth main character to add to the admin group.",
        )
        parser.add_argument(
            "--site-wide",
            action="store_true",
            help="Also grant the admin group access to every Auth character, not only its corporation.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show planned changes without writing them.",
        )

    def _permissions(self):
        permissions = {
            permission.codename: permission
            for permission in Permission.objects.filter(
                content_type__app_label="buh_mining_analytics"
            )
        }
        expected = {
            "basic_access",
            "view_corporation",
            "view_all",
            "export_data",
            "manage_access",
        }
        missing = expected - permissions.keys()
        if missing:
            raise CommandError(
                "Mining Analytics permissions are missing. Run `auth migrate` first. "
                f"Missing: {', '.join(sorted(missing))}"
            )
        return permissions

    @transaction.atomic
    def handle(self, *args, **options):
        del args
        permissions = self._permissions()
        dry_run = options["dry_run"]
        member_group_name = options.get("member_group")
        admin_group_name = options["admin_group"]
        try:
            if not apps.is_installed("buh_structure_ops"):
                raise ImportError
            from buh_structure_ops.access_roles import selected_groups
        except (ImportError, RuntimeError):
            selected_groups = None
        if selected_groups and (not member_group_name or not admin_group_name):
            shared_member, shared_director = selected_groups()
            member_group_name = member_group_name or shared_member.name
            admin_group_name = admin_group_name or shared_director.name
        member_group_name = member_group_name or "Member"
        admin_group_name = admin_group_name or "Director"

        member_group = None
        if member_group_name:
            member_group = Group.objects.filter(name=member_group_name).first()
            if not member_group:
                self.stdout.write(
                    f'Would create member group: "{member_group_name}"'
                    if dry_run
                    else f'Creating member group: "{member_group_name}"'
                )
                if not dry_run:
                    member_group = Group.objects.create(name=member_group_name)

        admin_group = Group.objects.filter(name=admin_group_name).first()
        if not admin_group:
            self.stdout.write(
                f'Would create admin group: "{admin_group_name}"'
                if dry_run
                else f'Creating admin group: "{admin_group_name}"'
            )
            if not dry_run:
                admin_group = Group.objects.create(name=admin_group_name)

        if member_group:
            member_codes = ("basic_access", "export_data")
            self.stdout.write(
                f'Granting {", ".join(member_codes)} to "{member_group.name}"'
            )
            if not dry_run:
                member_group.permissions.add(
                    *(permissions[code] for code in member_codes)
                )

        admin_codes = [
            "basic_access",
            "view_corporation",
            "view_all",
            "export_data",
            "manage_access",
        ]
        self.stdout.write(f'Granting {", ".join(admin_codes)} to "{admin_group_name}"')
        if not dry_run:
            admin_group.permissions.add(*(permissions[code] for code in admin_codes))

        admin_character_name = options.get("admin_character")
        if admin_character_name:
            try:
                eve_character = EveCharacter.objects.select_related(
                    "character_ownership__user"
                ).get(character_name__iexact=admin_character_name)
                user = eve_character.character_ownership.user
            except EveCharacter.DoesNotExist as exc:
                raise CommandError(
                    f'Character "{admin_character_name}" is not known to Auth.'
                ) from exc
            except AttributeError as exc:
                raise CommandError(
                    f'Character "{admin_character_name}" has no Auth owner.'
                ) from exc
            self.stdout.write(
                f'Adding Auth user for "{eve_character.character_name}" to "{admin_group_name}"'
            )
            if not dry_run:
                user.groups.add(admin_group)

        if dry_run:
            transaction.set_rollback(True)
            self.stdout.write(
                self.style.WARNING("Dry run complete. No changes were made.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS("Mining Analytics permissions are ready.")
            )
