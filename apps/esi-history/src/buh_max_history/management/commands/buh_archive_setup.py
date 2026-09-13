from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from buh_max_history.access import PERMISSION_CODES
from buh_max_history.models import ArchiveConfiguration, PublicDataset

DEFAULT_DATASETS = (
    (
        "EVE Ref reference data",
        "reference-data",
        "https://data.everef.net/reference-data/index.json",
        10,
    ),
    (
        "EVE Ref Hoboleaks SDE",
        "hoboleaks-sde",
        "https://data.everef.net/hoboleaks-sde/index.json",
        20,
    ),
    (
        "EVE Ref ESI scrape",
        "esi-scrape",
        "https://data.everef.net/esi-scrape/index.json",
        30,
    ),
    (
        "EVE Ref structures",
        "structures",
        "https://data.everef.net/structures/index.json",
        40,
    ),
    ("EVE Ref wars", "wars", "https://data.everef.net/wars/index.json", 50),
    (
        "EVE Ref public contracts",
        "public-contracts",
        "https://data.everef.net/public-contracts/index.json",
        60,
    ),
    (
        "EVE Ref market orders",
        "market-orders",
        "https://data.everef.net/market-orders/index.json",
        70,
    ),
    (
        "EVE Ref market history",
        "market-history",
        "https://data.everef.net/market-history/index.json",
        80,
    ),
    ("EVE Ref killmails", "killmails", "https://data.everef.net/killmails/index.json", 90),
)


def all_archive_permissions():
    permissions = Permission.objects.filter(
        content_type__app_label="buh_max_history",
        codename__in=PERMISSION_CODES,
    )
    if permissions.count() != len(PERMISSION_CODES):
        found = set(permissions.values_list("codename", flat=True))
        missing = sorted(set(PERMISSION_CODES) - found)
        raise CommandError(
            "ESI History Archive permissions are missing: "
            f"{', '.join(missing)}. Run migrations first."
        )
    return permissions


def configured_director_group(requested_name: str):
    if requested_name:
        return Group.objects.filter(name__iexact=requested_name).first()
    try:
        from buh_structure_ops.models import AccessRoleConfiguration

        role_config = (
            AccessRoleConfiguration.objects.select_related("director_group")
            .filter(singleton_id=1)
            .first()
        )
        if role_config and role_config.director_group_id:
            return role_config.director_group
    except (ImportError, LookupError):
        pass
    return Group.objects.filter(name__iexact="Director").first()


class Command(BaseCommand):
    help = "Initialize archive settings, EVE Ref datasets, and Director access."

    def add_arguments(self, parser):
        parser.add_argument("--admin-username", default="")
        parser.add_argument("--director-group", default="")
        parser.add_argument("--repair-default-access", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        del args
        config, created = ArchiveConfiguration.objects.get_or_create(singleton_id=1)
        dataset_created = 0
        for name, slug, index_url, priority in DEFAULT_DATASETS:
            _, was_created = PublicDataset.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "index_url": index_url,
                    "priority": priority,
                    "enabled": True,
                },
            )
            dataset_created += int(was_created)

        director = configured_director_group(options["director_group"].strip())
        if not director:
            raise CommandError(
                "The existing Director Auth group was not found. Pass --director-group."
            )
        permissions = list(all_archive_permissions())
        already_configured = director.permissions.filter(
            content_type__app_label="buh_max_history"
        ).exists()
        if not already_configured or options["repair_default_access"]:
            director.permissions.add(*permissions)
            access_result = f'Granted all archive permissions to "{director.name}".'
        else:
            access_result = (
                f'Preserved customized archive permissions on "{director.name}".'
            )

        username = options["admin_username"].strip()
        if username:
            user = get_user_model().objects.filter(username__iexact=username).first()
            if not user:
                raise CommandError(f'Auth user "{username}" was not found.')
            user.groups.add(director)
            self.stdout.write(f'Confirmed {user.username} in "{director.name}".')

        self.stdout.write(
            f"Archive settings {'created' if created else 'already exist'}; "
            f"public mirror={'enabled' if config.public_mirror_enabled else 'disabled'}."
        )
        self.stdout.write(f"Created {dataset_created} default EVE Ref dataset(s).")
        self.stdout.write(access_result)
        if options["dry_run"]:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING("Dry run complete. No changes were made."))
        else:
            self.stdout.write(self.style.SUCCESS("B-UH ESI History Archive is ready."))
