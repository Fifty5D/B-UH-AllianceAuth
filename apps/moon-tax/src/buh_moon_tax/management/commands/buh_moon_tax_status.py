"""Concise operational status for installers and human diagnostics."""

from django.core.management.base import BaseCommand
from django.db.models import Sum

from buh_moon_tax.models import (
    ZERO,
    AuditRun,
    CompressionRule,
    MiningLine,
    PaymentCandidate,
    PaymentRecipient,
    StructureTaxPolicy,
    TaxAssessment,
    TaxConfiguration,
)


class Command(BaseCommand):
    help = "Show Moon Tax source, audit, payment, and balance readiness."

    def handle(self, *args, **options):
        del args, options
        config = TaxConfiguration.objects.filter(singleton_id=1).first()
        latest = AuditRun.objects.order_by("-queued_at").first()
        totals = TaxAssessment.objects.aggregate(
            tax=Sum("tax_due"), paid=Sum("approved_paid")
        )
        tax = totals["tax"] or ZERO
        paid = totals["paid"] or ZERO
        self.stdout.write("B-UH MOON TAX STATUS")
        self.stdout.write(f"  configuration: {'ready' if config else 'MISSING'}")
        if config:
            self.stdout.write(f"  default rate: {config.default_tax_rate}%")
            self.stdout.write(f"  mining window: {config.mining_window_days} days")
            recipients = list(
                config.default_payment_recipients.filter(enabled=True).values_list(
                    "name", flat=True
                )
            )
            self.stdout.write(
                "  default recipients: "
                + (", ".join(recipients) if recipients else "NONE")
            )
            self.stdout.write(
                f"  recipient directory: {PaymentRecipient.objects.count()} available"
            )
            self.stdout.write(
                f"  per-Athanor policies: {StructureTaxPolicy.objects.count()}"
            )
            self.stdout.write(
                f"  enforcement: {'ENABLED' if config.enforcement_enabled else 'disabled'}"
            )
        self.stdout.write(f"  compression rules: {CompressionRule.objects.count()}")
        self.stdout.write(f"  preserved mining lines: {MiningLine.objects.count()}")
        self.stdout.write(f"  tax assessed: {tax:,.2f} ISK")
        self.stdout.write(f"  approved paid: {paid:,.2f} ISK")
        self.stdout.write(f"  outstanding: {max(ZERO, tax - paid):,.2f} ISK")
        self.stdout.write(
            "  pending payment candidates: "
            + str(
                PaymentCandidate.objects.filter(
                    status=PaymentCandidate.Status.PENDING
                ).count()
            )
        )
        self.stdout.write(
            "  payments excluded by exemption: "
            + str(
                PaymentCandidate.objects.filter(
                    status=PaymentCandidate.Status.EXEMPT
                ).count()
            )
        )
        if latest:
            self.stdout.write(
                f"  latest audit: #{latest.pk} {latest.get_status_display()} "
                f"({latest.queued_at.isoformat()})"
            )
            if latest.error:
                self.stdout.write(self.style.ERROR(f"  error: {latest.error}"))
            for warning in latest.warnings[:10]:
                self.stdout.write(self.style.WARNING(f"  warning: {warning}"))
        else:
            self.stdout.write("  latest audit: never")
