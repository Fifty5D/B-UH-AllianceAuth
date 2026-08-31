import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.utils.timezone import now

from buh_moon_tax.models import PaymentCandidate, TaxAssessment, TaxPeriod
from buh_moon_tax.payments import decide_payment


class PaymentDecisionConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        if connection.vendor != "mysql":
            self.skipTest("This locking test is intentionally MariaDB-only.")
        self.director = get_user_model().objects.create_user("concurrency-director")
        self.member = get_user_model().objects.create_user("concurrency-member")
        stamp = now() - dt.timedelta(days=5)
        period = TaxPeriod.objects.create(
            source_started_at=stamp - dt.timedelta(days=20),
            structure_id=77000001,
            structure_name="Test Concurrent Athanor",
            corporation_id=98000001,
            corporation_name="B-UH",
            extraction_started_at=stamp - dt.timedelta(days=20),
            pop_at=stamp,
            closes_at=stamp + dt.timedelta(days=3),
            due_at=stamp + dt.timedelta(days=3),
            tax_rate=Decimal("5"),
            allowed_recipients=[{"kind": "CHARACTER", "id": 99000001, "name": "Payee"}],
            status=TaxPeriod.Status.DUE,
        )
        TaxAssessment.objects.create(
            period=period,
            billing_key=f"user:{self.member.pk}",
            auth_user=self.member,
            display_name="Concurrency Member",
            gross_value=Decimal("200000000"),
            calculated_tax_due=Decimal("10000000"),
            tax_due=Decimal("10000000"),
            due_at=period.due_at,
            status=TaxAssessment.Status.DUE,
        )
        self.payment = PaymentCandidate.objects.create(
            source=PaymentCandidate.Source.WALLET,
            source_key="integration:concurrency:payment",
            payer_character_id=99000002,
            payer_character_name="Concurrency Member",
            auth_user=self.member,
            recipient_id=99000001,
            recipient_name="Payee",
            amount=Decimal("12000000"),
            occurred_at=now(),
        )

    def test_two_director_decisions_never_duplicate_the_payment(self):
        barrier = Barrier(2)

        def decide(disposition):
            close_old_connections()
            barrier.wait(timeout=5)
            payment = PaymentCandidate.objects.get(pk=self.payment.pk)
            director = get_user_model().objects.get(pk=self.director.pk)
            result = decide_payment(
                payment,
                disposition=disposition,
                director=director,
                note=f"Concurrent {disposition}",
            )
            close_old_connections()
            return result.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(decide, ("oldest_carryover", "donation")))

        payment = PaymentCandidate.objects.get(pk=self.payment.pk)
        allocations = list(payment.allocations.all())
        self.assertIn(payment.status, statuses)
        self.assertEqual(sum(item.amount for item in allocations), payment.amount)
        self.assertLessEqual(len(allocations), 2)
