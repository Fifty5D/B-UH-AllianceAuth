import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.utils.timezone import now

from buh_moon_tax.admin import PaymentCandidateAdmin
from buh_moon_tax.models import (
    AssessmentAdjustment,
    PaymentAllocation,
    PaymentCandidate,
    TaxAssessment,
    TaxConfiguration,
    TaxExemption,
    TaxPeriod,
)
from buh_moon_tax.payments import (
    create_assessment_adjustment,
    decide_payment,
    recalculate_assessment,
    reconcile_payment_exemptions,
    reverse_assessment_adjustment,
)


class DirectorPaymentDecisionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.director = get_user_model().objects.create_user("director")
        cls.payer = get_user_model().objects.create_user("payer")
        TaxConfiguration.objects.create(
            singleton_id=1, notify_member_payment_decision=False
        )
        base = now() - dt.timedelta(days=10)
        cls.period_1 = TaxPeriod.objects.create(
            source_started_at=base - dt.timedelta(days=20),
            structure_id=1,
            structure_name="Athanor One",
            corporation_id=10,
            corporation_name="B-UH",
            extraction_started_at=base - dt.timedelta(days=20),
            pop_at=base,
            closes_at=base + dt.timedelta(days=3),
            due_at=base + dt.timedelta(days=3),
            tax_rate=Decimal(5),
            allowed_recipients=[
                {"kind": "CHARACTER", "id": 55, "name": "Fifty5D"}
            ],
            status=TaxPeriod.Status.DUE,
        )
        cls.period_2 = TaxPeriod.objects.create(
            source_started_at=base - dt.timedelta(days=5),
            structure_id=2,
            structure_name="Athanor Two",
            corporation_id=10,
            corporation_name="B-UH",
            extraction_started_at=base - dt.timedelta(days=5),
            pop_at=base + dt.timedelta(days=5),
            closes_at=base + dt.timedelta(days=8),
            due_at=base + dt.timedelta(days=8),
            tax_rate=Decimal(5),
            allowed_recipients=[
                {"kind": "CHARACTER", "id": 55, "name": "Fifty5D"}
            ],
            status=TaxPeriod.Status.DUE,
        )
        cls.bill_1 = TaxAssessment.objects.create(
            period=cls.period_1,
            billing_key=f"user:{cls.payer.pk}",
            auth_user=cls.payer,
            display_name="Payer",
            gross_value=Decimal(2000),
            tax_due=Decimal(100),
            due_at=cls.period_1.due_at,
            status=TaxAssessment.Status.DUE,
        )
        cls.bill_2 = TaxAssessment.objects.create(
            period=cls.period_2,
            billing_key=f"user:{cls.payer.pk}",
            auth_user=cls.payer,
            display_name="Payer",
            gross_value=Decimal(1000),
            tax_due=Decimal(50),
            due_at=cls.period_2.due_at,
            status=TaxAssessment.Status.DUE,
        )

    def payment(self, amount):
        return PaymentCandidate.objects.create(
            source=PaymentCandidate.Source.WALLET,
            source_key=f"wallet:1:{amount}",
            payer_character_id=1,
            payer_character_name="Payer Alt",
            auth_user=self.payer,
            recipient_id=55,
            recipient_name="Fifty5D",
            amount=Decimal(amount),
            occurred_at=now(),
        )

    def test_oldest_first_can_split_and_carry_overage(self):
        payment = self.payment("175")
        decide_payment(
            payment, disposition="oldest_carryover", director=self.director
        )
        allocations = list(payment.allocations.order_by("created_at"))
        self.assertEqual(
            [(item.category, item.amount) for item in allocations],
            [
                (PaymentAllocation.Category.TAX, Decimal(100)),
                (PaymentAllocation.Category.TAX, Decimal(50)),
                (PaymentAllocation.Category.CARRYOVER, Decimal(25)),
            ],
        )
        self.bill_1.refresh_from_db()
        self.bill_2.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(self.bill_1.status, TaxAssessment.Status.PAID)
        self.assertEqual(self.bill_2.status, TaxAssessment.Status.PAID)
        self.assertEqual(payment.status, PaymentCandidate.Status.APPROVED)

    def test_reject_never_changes_a_bill(self):
        payment = self.payment("25")
        decide_payment(payment, disposition="reject", director=self.director)
        self.assertFalse(payment.allocations.exists())
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentCandidate.Status.REJECTED)
        self.bill_1.refresh_from_db()
        self.assertEqual(self.bill_1.approved_paid, Decimal(0))

    def test_admin_custom_split_removal_repairs_the_previous_bill(self):
        payment = self.payment("50")
        decide_payment(
            payment,
            disposition="oldest_carryover",
            director=self.director,
        )
        allocation = payment.allocations.get(
            category=PaymentAllocation.Category.TAX
        )
        self.bill_1.refresh_from_db()
        self.assertEqual(self.bill_1.approved_paid, Decimal("50.00"))

        formset = Mock()
        formset.save.return_value = []
        formset.deleted_objects = [allocation]
        request = RequestFactory().post("/admin/moon-tax/payment/")
        request.user = self.director
        PaymentCandidateAdmin(PaymentCandidate, admin.site).save_formset(
            request,
            SimpleNamespace(instance=payment),
            formset,
            change=True,
        )

        self.bill_1.refresh_from_db()
        self.period_1.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(self.bill_1.approved_paid, Decimal("0.00"))
        self.assertEqual(self.period_1.approved_paid, Decimal("0.00"))
        self.assertEqual(payment.status, PaymentCandidate.Status.PENDING)
        formset.save_m2m.assert_called_once_with()

    def test_redeciding_a_payment_is_locked_and_never_duplicates_allocations(self):
        payment = self.payment("25")
        payment_manager = PaymentCandidate.objects
        assessment_manager = TaxAssessment.objects
        with patch.object(
            payment_manager,
            "select_for_update",
            wraps=payment_manager.select_for_update,
        ) as payment_lock, patch.object(
            assessment_manager,
            "select_for_update",
            wraps=assessment_manager.select_for_update,
        ) as assessment_lock:
            decide_payment(
                payment,
                disposition="oldest_carryover",
                director=self.director,
            )
        self.assertGreaterEqual(payment_lock.call_count, 1)
        self.assertGreaterEqual(assessment_lock.call_count, 1)

        decide_payment(
            payment,
            disposition="oldest_carryover",
            director=self.director,
        )

        allocations = payment.allocations.all()
        self.assertEqual(allocations.count(), 1)
        self.assertEqual(allocations.get().amount, Decimal("25.00"))

    def test_locked_exempt_payment_cannot_be_reclassified(self):
        payment = self.payment("25")
        PaymentCandidate.objects.filter(pk=payment.pk).update(
            status=PaymentCandidate.Status.EXEMPT,
            review_note="[AUTO-EXEMPT] Protected by audit",
        )
        self.assertEqual(payment.status, PaymentCandidate.Status.PENDING)

        with self.assertRaisesMessage(ValueError, "cannot be manually reclassified"):
            decide_payment(
                payment,
                disposition="oldest_carryover",
                director=self.director,
            )

        payment.refresh_from_db()
        self.assertEqual(payment.status, PaymentCandidate.Status.EXEMPT)
        self.assertFalse(payment.allocations.exists())

    def test_wrong_athanor_recipient_cannot_be_allocated(self):
        payment = PaymentCandidate.objects.create(
            source=PaymentCandidate.Source.WALLET,
            source_key="wallet:wrong-recipient",
            payer_character_id=1,
            payer_character_name="Payer Alt",
            auth_user=self.payer,
            recipient_id=999,
            recipient_name="Not allowed for this Athanor",
            amount=Decimal(25),
            occurred_at=now(),
        )
        allocation = PaymentAllocation(
            payment=payment,
            assessment=self.bill_1,
            category=PaymentAllocation.Category.TAX,
            amount=Decimal(25),
        )
        with self.assertRaisesMessage(
            ValidationError, "destination was not allowed"
        ):
            allocation.full_clean()

    def test_manual_credit_can_override_a_destination(self):
        payment = PaymentCandidate.objects.create(
            source=PaymentCandidate.Source.MANUAL,
            source_key="manual:test",
            payer_character_id=1,
            payer_character_name="Payer Alt",
            auth_user=self.payer,
            recipient_id=0,
            recipient_name="Director credit",
            amount=Decimal(25),
            occurred_at=now(),
        )
        allocation = PaymentAllocation(
            payment=payment,
            assessment=self.bill_1,
            category=PaymentAllocation.Category.TAX,
            amount=Decimal(25),
        )
        allocation.full_clean()

    def test_account_exemption_covers_every_linked_character_payment(self):
        characters = []
        for offset in (1, 2):
            character = EveCharacter.objects.create(
                character_id=92000000 + offset,
                character_name=f"Exempt Alt {offset}",
                corporation_id=98000001,
                corporation_name="B-UH",
            )
            CharacterOwnership.objects.create(
                character=character,
                owner_hash=f"exempt-owner-{offset}",
                user=self.payer,
            )
            characters.append(character)
        first = PaymentCandidate.objects.create(
            source=PaymentCandidate.Source.WALLET,
            source_key="wallet:exempt-alt-1",
            payer_character_id=characters[0].character_id,
            payer_character_name=characters[0].character_name,
            auth_user=self.payer,
            recipient_id=55,
            recipient_name="Fifty5D",
            amount=Decimal(10),
            occurred_at=now(),
        )
        second = PaymentCandidate.objects.create(
            source=PaymentCandidate.Source.CONTRACT,
            source_key="contract:exempt-alt-2",
            payer_character_id=characters[1].character_id,
            payer_character_name=characters[1].character_name,
            recipient_id=55,
            recipient_name="Fifty5D",
            amount=Decimal(20),
            occurred_at=now(),
        )
        exemption = TaxExemption.objects.create(
            auth_user=self.payer,
            effective_from=now().date(),
            reason="All linked alts are exempt",
        )

        result = reconcile_payment_exemptions()

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(result["excluded"], 2)
        self.assertEqual(first.status, PaymentCandidate.Status.EXEMPT)
        self.assertEqual(second.status, PaymentCandidate.Status.EXEMPT)
        self.assertEqual(second.auth_user, self.payer)
        self.assertTrue(first.review_note.startswith("[AUTO-EXEMPT]"))

        exemption.active = False
        exemption.save(update_fields=("active",))
        result = reconcile_payment_exemptions()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(result["restored"], 2)
        self.assertEqual(first.status, PaymentCandidate.Status.PENDING)
        self.assertEqual(second.status, PaymentCandidate.Status.PENDING)

    def test_retroactive_account_exemption_unwinds_approved_tax(self):
        payment = self.payment("50")
        decide_payment(
            payment,
            disposition="oldest_carryover",
            director=self.director,
        )
        self.bill_1.refresh_from_db()
        self.assertEqual(self.bill_1.approved_paid, Decimal(50))

        TaxExemption.objects.create(
            auth_user=self.payer,
            effective_from=payment.occurred_at.date(),
            reason="Retroactive director exemption",
        )
        reconcile_payment_exemptions()

        payment.refresh_from_db()
        self.bill_1.refresh_from_db()
        self.assertEqual(payment.status, PaymentCandidate.Status.EXEMPT)
        self.assertFalse(payment.allocations.exists())
        self.assertEqual(self.bill_1.approved_paid, Decimal(0))
        evidence = payment.evidence["moon_tax_exemption"]
        self.assertEqual(evidence["previous_status"], PaymentCandidate.Status.APPROVED)
        self.assertEqual(evidence["removed_allocations"][0]["amount"], "50.00")

    def test_small_price_variance_waiver_is_reversible(self):
        payment = self.payment("99")
        decide_payment(
            payment,
            disposition="oldest_carryover",
            director=self.director,
        )
        self.bill_1.refresh_from_db()
        self.assertEqual(self.bill_1.outstanding, Decimal("1.00"))

        adjustment = create_assessment_adjustment(
            self.bill_1,
            action="WAIVE_BALANCE",
            amount=None,
            reason="Small Jita price variance",
            director=self.director,
        )
        self.bill_1.refresh_from_db()
        self.assertEqual(adjustment.kind, AssessmentAdjustment.Kind.WAIVER)
        self.assertEqual(adjustment.amount, Decimal("-1.00"))
        self.assertEqual(self.bill_1.calculated_tax_due, Decimal("100.00"))
        self.assertEqual(self.bill_1.tax_due, Decimal("99.00"))
        self.assertEqual(self.bill_1.outstanding, Decimal("0.00"))
        self.assertEqual(self.bill_1.status, TaxAssessment.Status.PAID)

        # A later audit recalculation keeps the adjustment layer in place.
        recalculate_assessment(self.bill_1)
        self.bill_1.refresh_from_db()
        self.assertEqual(self.bill_1.tax_due, Decimal("99.00"))

        reverse_assessment_adjustment(
            adjustment,
            reason="Director correction",
            director=self.director,
        )
        self.bill_1.refresh_from_db()
        self.assertEqual(self.bill_1.tax_due, Decimal("100.00"))
        self.assertEqual(self.bill_1.outstanding, Decimal("1.00"))
        self.assertEqual(self.bill_1.status, TaxAssessment.Status.PARTIAL)

    def test_void_preserves_bill_and_original_calculation(self):
        adjustment = create_assessment_adjustment(
            self.bill_1,
            action="VOID_BILL",
            amount=None,
            reason="Incorrect extraction assignment",
            director=self.director,
        )
        self.bill_1.refresh_from_db()
        self.assertTrue(TaxAssessment.objects.filter(pk=self.bill_1.pk).exists())
        self.assertEqual(adjustment.kind, AssessmentAdjustment.Kind.VOID)
        self.assertEqual(self.bill_1.calculated_tax_due, Decimal("100.00"))
        self.assertEqual(self.bill_1.tax_due, Decimal("0.00"))
        self.assertEqual(self.bill_1.status, TaxAssessment.Status.WAIVED)

    def test_overpayment_on_one_bill_does_not_settle_another_accounts_debt(self):
        other = get_user_model().objects.create_user("other-period-payer")
        other_bill = TaxAssessment.objects.create(
            period=self.period_1,
            billing_key=f"user:{other.pk}",
            auth_user=other,
            display_name="Other payer",
            gross_value=Decimal("800.00"),
            calculated_tax_due=Decimal("40.00"),
            tax_due=Decimal("40.00"),
            due_at=self.period_1.due_at,
            status=TaxAssessment.Status.DUE,
        )
        payment = self.payment("90")
        decide_payment(
            payment,
            disposition="oldest_carryover",
            director=self.director,
        )

        create_assessment_adjustment(
            self.bill_1,
            action="CREDIT",
            amount=Decimal("50.00"),
            reason="Director correction after partial payment",
            director=self.director,
        )

        self.bill_1.refresh_from_db()
        other_bill.refresh_from_db()
        self.period_1.refresh_from_db()
        self.assertEqual(self.bill_1.tax_due, Decimal("50.00"))
        self.assertEqual(self.bill_1.approved_paid, Decimal("90.00"))
        self.assertEqual(other_bill.outstanding, Decimal("40.00"))
        self.assertEqual(self.period_1.status, TaxPeriod.Status.DUE)
