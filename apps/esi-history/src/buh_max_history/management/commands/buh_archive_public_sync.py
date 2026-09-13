import json

from django.core.management.base import BaseCommand, CommandError

from buh_max_history.models import ArchiveConfiguration, ArchiveJob
from buh_max_history.tasks import _run_job


class Command(BaseCommand):
    help = "Catalog or perform one bounded, resumable EVE Ref public archive sync."

    def add_arguments(self, parser):
        parser.add_argument("--catalog-only", action="store_true")
        parser.add_argument("--no-catalog", action="store_true")
        parser.add_argument("--enable", action="store_true")
        parser.add_argument("--disable", action="store_true")

    def handle(self, *args, **options):
        del args
        if options["enable"] and options["disable"]:
            raise CommandError("Choose either --enable or --disable, not both.")
        config = ArchiveConfiguration.get_solo()
        if options["enable"] or options["disable"]:
            config.public_mirror_enabled = bool(options["enable"])
            config.save(update_fields=("public_mirror_enabled", "updated_at"))
            self.stdout.write(
                f"Public mirror {'enabled' if config.public_mirror_enabled else 'disabled'}."
            )
        if options["disable"]:
            return
        job = ArchiveJob.objects.create(
            kind=ArchiveJob.Kind.CATALOG
            if options["catalog_only"]
            else ArchiveJob.Kind.SYNC
        )
        result = _run_job(job, catalog_first=not options["no_catalog"])
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True))
        if not result["ok"]:
            raise CommandError(
                "Public archive operation needs attention; see output above."
            )
