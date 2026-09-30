from buh_structure_ops.models import AccessRoleConfiguration
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from buh_vps_health.access import PERMISSION_CODES
from buh_vps_health.models import HealthConfiguration


def health_permissions():
    permissions = Permission.objects.filter(
        content_type__app_label="buh_vps_health",
        codename__in=PERMISSION_CODES,
    )
    if permissions.count() != len(PERMISSION_CODES):
        found = set(permissions.values_list("codename", flat=True))
        missing = sorted(set(PERMISSION_CODES) - found)
        raise CommandError(
            "VPS Health permissions are missing: "
            f"{', '.join(missing)}. Run migrations first."
        )
    return permissions


class Command(BaseCommand):
    help = "Initialize VPS Health and grant its full starter access to Director."

    def add_arguments(self, parser):
        parser.add_argument("--admin-username", default="")
        parser.add_argument("--director-group", default="")
        parser.add_argument("--repair-default-access", action="store_true")
        parser.add_argument("--grant-update-access", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        del args
        config, config_created = HealthConfiguration.objects.get_or_create(
            singleton_id=1
        )
        role_config = (
            AccessRoleConfiguration.objects.select_related("director_group")
            .filter(singleton_id=1)
            .first()
        )
        requested_name = options["director_group"].strip()
        if requested_name:
            director = Group.objects.filter(name__iexact=requested_name).first()
        elif role_config and role_config.director_group_id:
            director = role_config.director_group
        else:
            director = Group.objects.filter(name__iexact="Director").first()
        if not director:
            raise CommandError(
                "The existing Director Auth group was not found. Configure the "
                "Structure Operations Director role or pass --director-group."
            )

        permissions = list(health_permissions())
        already_configured = director.permissions.filter(
            content_type__app_label="buh_vps_health"
        ).exists()
        if not already_configured or options["repair_default_access"]:
            director.permissions.add(*permissions)
            access_result = f'Granted all VPS Health permissions to "{director.name}".'
        else:
            access_result = (
                f'Preserved the customized VPS Health permissions on "{director.name}".'
            )
            if options["grant_update_access"]:
                update_permissions = [
                    item
                    for item in permissions
                    if item.codename in {"check_updates", "view_update_results"}
                ]
                director.permissions.add(*update_permissions)
                access_result += " Granted the two new update-check permissions."

        username = options["admin_username"].strip()
        if username:
            user = director.user_set.model.objects.filter(
                username__iexact=username
            ).first()
            if not user:
                raise CommandError(f'Auth user "{username}" was not found.')
            user.groups.add(director)
            self.stdout.write(f'Confirmed {user.username} in "{director.name}".')

        self.stdout.write(
            f"Health settings {'created' if config_created else 'already exist'}: "
            f"refresh={config.live_refresh_seconds}s, workers="
            f"{config.worker_minimum}-{config.worker_maximum}."
        )
        self.stdout.write(access_result)
        if options["dry_run"]:
            transaction.set_rollback(True)
            self.stdout.write(
                self.style.WARNING("Dry run complete. No changes were made.")
            )
        else:
            self.stdout.write(self.style.SUCCESS("B-UH VPS Health setup is ready."))
