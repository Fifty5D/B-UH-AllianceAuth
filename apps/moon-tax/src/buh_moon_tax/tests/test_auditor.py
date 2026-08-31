import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from django.test import TestCase
from django.utils.timezone import now
from moonmining.models import MiningLedgerRecord
from moonmining.tests.testdata.factories import (
    ExtractionFactory,
    MiningLedgerRecordFactory,
)

from buh_moon_tax.auditor import reconcile
from buh_moon_tax.models import (
    AuditRun,
    CompressionRule,
    MiningLine,
    PaymentRecipient,
    StructureTaxPolicy,
    TaxAssessment,
    TaxConfiguration,
    TaxExemption,
    TaxPeriod,
)
from buh_moon_tax.payments import create_assessment_adjustment


class ExtractionReconciliationTests(TestCase):
    def setUp(self):
        self.current = now().replace(microsecond=0)
        self.extraction = ExtractionFactory(
            started_at=self.current - dt.timedelta(days=25),
            chunk_arrival_at=self.current - dt.timedelta(days=6),
            fractured_at=self.current - dt.timedelta(days=5),
            auto_fracture_at=self.current - dt.timedelta(days=5, hours=-3),
        )
        self.user = self.extraction.refinery.owner.character_ownership.user
        self.ledger = MiningLedgerRecordFactory(
            refinery=self.extraction.refinery,
            day=(self.current - dt.timedelta(days=4)).date(),
            quantity=1000,
            user=self.user,
        )
        CompressionRule.objects.create(
            raw_type_id=self.ledger.ore_type_id,
            raw_type_name=self.ledger.ore_type.name,
            compressed_type_id=999001,
            compressed_type_name=f"Compressed {self.ledger.ore_type.name}",
            raw_units=1,
            compressed_units=1,
        )
        self.config = TaxConfiguration.objects.create(
            singleton_id=1,
            default_tax_rate=Decimal(5),
            mining_window_days=3,
            notify_member_new_tax=False,
            notify_director_unpaid=False,
            notify_director_missing_data=False,
        )
        self.recipient = PaymentRecipient.objects.create(
            kind=PaymentRecipient.Kind.CHARACTER,
            entity_id=955001,
            name="Fifty5D",
        )
        self.config.default_payment_recipients.add(self.recipient)

    @staticmethod
    def fake_snapshots(audit, needs):
        return (
            {
                type_id: SimpleNamespace(
                    weighted_unit_price=Decimal(10), complete=True
                )
                for type_id in needs
            },
            [],
        )

    def run_audit(self):
        audit = AuditRun.objects.create(trigger=AuditRun.Trigger.MANUAL)
        with patch(
            "buh_moon_tax.auditor.create_price_snapshots",
            side_effect=self.fake_snapshots,
        ):
            reconcile(audit)
        return audit

    def test_builds_one_period_and_combined_account_bill(self):
        self.run_audit()
        period = TaxPeriod.objects.get()
        line = MiningLine.objects.get()
        bill = TaxAssessment.objects.get()
        self.assertEqual(period.pop_at, self.extraction.fractured_at)
        self.assertEqual(
            period.closes_at,
            self.extraction.fractured_at + dt.timedelta(days=3),
        )
        self.assertEqual(line.compressed_quantity, Decimal(1000))
        self.assertEqual(line.gross_value, Decimal(10000))
        self.assertEqual(line.tax_due, Decimal(500))
        self.assertEqual(bill.auth_user, self.user)
        self.assertEqual(bill.tax_due, Decimal(500))

    def test_effective_dated_exemption_recalculates_tax(self):
        TaxExemption.objects.create(
            auth_user=self.user,
            effective_from=self.ledger.day,
            reason="Director-approved test exemption",
        )
        self.run_audit()
        line = MiningLine.objects.get()
        bill = TaxAssessment.objects.get()
        self.assertTrue(line.is_exempt)
        self.assertEqual(line.tax_due, Decimal(0))
        self.assertEqual(bill.status, TaxAssessment.Status.EXEMPT)

    def test_preserves_imported_history_after_source_window_drops_it(self):
        first = self.run_audit()
        MiningLedgerRecord.objects.all().delete()
        second = self.run_audit()
        self.assertEqual(MiningLine.objects.count(), 1)
        self.assertTrue(first.summary["full_exemption_history_recheck"])
        self.assertFalse(second.summary["full_exemption_history_recheck"])
        self.assertEqual(second.summary["mining_exemptions_updated"], 0)

    def test_each_audit_rechecks_exemptions_on_preserved_history(self):
        self.run_audit()
        MiningLedgerRecord.objects.all().delete()
        TaxExemption.objects.create(
            auth_user=self.user,
            effective_from=self.ledger.day,
            reason="Exempt after the ESI row aged out",
        )

        audit = self.run_audit()

        line = MiningLine.objects.get()
        bill = TaxAssessment.objects.get()
        self.assertTrue(line.is_exempt)
        self.assertEqual(line.tax_due, Decimal(0))
        self.assertEqual(bill.status, TaxAssessment.Status.EXEMPT)
        self.assertEqual(audit.summary["mining_exemptions_updated"], 1)
        self.assertTrue(audit.summary["full_exemption_history_recheck"])

    def test_decided_unlinked_character_is_not_double_billed_after_linking(self):
        self.ledger.user = None
        self.ledger.save(update_fields=("user",))
        CharacterOwnership.objects.filter(
            character__character_id=self.ledger.character_id
        ).delete()
        self.run_audit()
        bill = TaxAssessment.objects.get()
        self.assertIsNone(bill.auth_user_id)
        self.assertEqual(bill.billing_key, f"character:{self.ledger.character_id}")
        create_assessment_adjustment(
            bill,
            action="DEBIT",
            amount=Decimal("10.00"),
            reason="Preserve a decided character bill",
            director=self.user,
        )

        character, _ = EveCharacter.objects.get_or_create(
            character_id=self.ledger.character_id,
            defaults={
                "character_name": str(self.ledger.character),
                "corporation_id": self.ledger.corporation_id,
                "corporation_name": str(self.ledger.corporation),
            },
        )
        CharacterOwnership.objects.create(
            character=character,
            owner_hash="newly-linked-miner",
            user=self.user,
        )

        self.run_audit()

        self.assertEqual(TaxAssessment.objects.count(), 1)
        bill.refresh_from_db()
        period = TaxPeriod.objects.get()
        self.assertEqual(bill.auth_user, self.user)
        self.assertFalse(bill.combined_characters)
        self.assertEqual(bill.gross_value, Decimal("10000.00"))
        self.assertEqual(bill.calculated_tax_due, Decimal("500.00"))
        self.assertEqual(bill.tax_due, Decimal("510.00"))
        self.assertEqual(period.gross_value, Decimal("10000.00"))
        self.assertEqual(period.tax_due, Decimal("510.00"))

    def test_closed_period_freezes_tax_rate_and_recipient_snapshot(self):
        self.run_audit()
        period = TaxPeriod.objects.get()
        self.assertEqual(period.tax_rate, Decimal(5))
        self.assertEqual(period.allowed_recipients[0]["id"], 955001)

        replacement = PaymentRecipient.objects.create(
            kind=PaymentRecipient.Kind.CORPORATION,
            entity_id=98000001,
            name="Replacement Corp",
        )
        self.config.default_payment_recipients.set([replacement])
        StructureTaxPolicy.objects.create(
            structure_id=self.extraction.refinery_id,
            structure_name=self.extraction.refinery.name,
            corporation_id=self.extraction.refinery.owner.corporation.corporation_id,
            corporation_name=self.extraction.refinery.owner.corporation.corporation_name,
            tax_rate=Decimal(25),
            effective_from=self.extraction.fractured_at - dt.timedelta(days=1),
        )
        self.run_audit()

        period.refresh_from_db()
        self.assertEqual(period.tax_rate, Decimal(5))
        self.assertEqual(period.allowed_recipients[0]["id"], 955001)
