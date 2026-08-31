"""Print a concise, support-safe service and launch-readiness report."""

from importlib.metadata import version

from django.core.management.base import BaseCommand
from moonmining.models import Extraction
from structures.models import Structure

from buh_structure_ops.models import (
    AccessRoleConfiguration,
    AlertEvent,
    ScheduleEvent,
    StructureSnapshot,
    WarState,
)
from buh_structure_ops.services import readiness_report


class Command(BaseCommand):
    help = "Show Structure Operations package, authorization, data, and alert status."

    def handle(self, *args, **options):
        del args, options
        report = readiness_report()
        self.stdout.write("B-UH Structure Operations status")
        for package in (
            "allianceauth",
            "django-esi",
            "aa-structures",
            "aa-moonmining",
            "aa-buh-structure-ops",
        ):
            self.stdout.write(f"  {package}: {version(package)}")
        self.stdout.write("")
        self.stdout.write(
            f"Launch readiness: {'READY' if report['is_ready'] else 'INCOMPLETE'} "
            f"({report['ready_count']}/{report['required_count']} corporations)"
        )
        for row in report["rows"]:
            self.stdout.write(
                f"  {row.corporation_name} [{row.corporation_id}] "
                f"structures={'OK' if row.structure_owner else 'MISSING'} "
                f"extractions={'OK' if row.refinery_owner else 'MISSING'} "
                f"director={row.structure_character or row.refinery_character or '-'}"
            )
        self.stdout.write("")
        self.stdout.write(f"Structures stored: {Structure.objects.count()}")
        self.stdout.write(
            "Active extractions: "
            f"{Extraction.objects.filter(status__in=Extraction.Status.considered_active()).count()}"
        )
        self.stdout.write(f"Fuel snapshots: {StructureSnapshot.objects.count()}")
        self.stdout.write(
            f"Active wars: {WarState.objects.filter(is_active=True).count()}"
        )
        self.stdout.write(
            f"Active alerts: {AlertEvent.objects.filter(is_active=True).count()}"
        )
        self.stdout.write(
            "Unacknowledged alerts: "
            f"{AlertEvent.objects.filter(is_active=True, acknowledged_at__isnull=True).count()}"
        )
        self.stdout.write(f"Custom schedule events: {ScheduleEvent.objects.count()}")
        self.stdout.write("")
        config = AccessRoleConfiguration.objects.select_related(
            "member_group", "director_group"
        ).filter(singleton_id=1).first()
        if not config:
            self.stdout.write("Access role settings: NOT CONFIGURED")
            return
        for label, group in (
            ("Member defaults", config.member_group),
            ("Director defaults", config.director_group),
        ):
            if group is None:
                self.stdout.write(f"{label}: MISSING")
                continue
            self.stdout.write(
                f'{label} = "{group.name}": {group.user_set.count()} members, '
                f"{group.permissions.count()} direct permissions"
            )
