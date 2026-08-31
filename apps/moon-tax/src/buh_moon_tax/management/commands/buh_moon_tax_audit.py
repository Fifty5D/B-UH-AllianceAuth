"""Start, reconcile, or report Moon Tax audits from the Alliance Auth CLI."""

from django.core.management.base import BaseCommand

from buh_moon_tax.auditor import reconcile
from buh_moon_tax.models import AuditRun
from buh_moon_tax.tasks import create_audit, refresh_sources


class Command(BaseCommand):
    help = "Run a Moon Tax audit now or queue upstream source refreshes first."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-refresh",
            action="store_true",
            help="Reconcile immediately against stored source data.",
        )

    def handle(self, *args, **options):
        del args
        audit = create_audit(AuditRun.Trigger.MANUAL)
        if options["no_refresh"]:
            summary = reconcile(audit)
            self.stdout.write(self.style.SUCCESS(f"Audit #{audit.pk} complete."))
            for key, value in summary.items():
                self.stdout.write(f"  {key}: {value}")
            if audit.warnings:
                self.stdout.write(self.style.WARNING("Warnings:"))
                for warning in audit.warnings:
                    self.stdout.write(f"  - {warning}")
        else:
            refresh_sources.delay(audit.pk)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Audit #{audit.pk} queued. Use buh_moon_tax_status to monitor it."
                )
            )
