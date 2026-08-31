import datetime as dt
import re
from decimal import Decimal
from unittest.mock import patch

from allianceauth.eveonline.models import EveCharacter
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils.timezone import now

from buh_moon_tax.models import (
    AuditRun,
    BillingPreference,
    MiningLine,
    PaymentAllocation,
    PaymentCandidate,
    PaymentRecipient,
    StructureTaxPolicy,
    TaxAssessment,
    TaxConfiguration,
    TaxExemption,
    TaxPeriod,
)


class PermissionBoundaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.one = get_user_model().objects.create_user("member-one")
        cls.two = get_user_model().objects.create_user("member-two")
        for offset, user in enumerate((cls.one, cls.two), start=1):
            character = EveCharacter.objects.create(
                character_id=90000000 + offset,
                character_name=f"Test Pilot {offset}",
                corporation_id=98000001,
                corporation_name="Bureau of Unified Harvesting",
            )
            user.profile.main_character = character
            user.profile.save(update_fields=["main_character"])
        permissions = Permission.objects.filter(
            content_type__app_label="buh_moon_tax",
            codename__in=("view_moon_tax", "view_own_tax", "view_mining_values"),
        )
        cls.one.user_permissions.add(*permissions)
        base = now() - dt.timedelta(days=6)
        cls.period = TaxPeriod.objects.create(
            source_started_at=base - dt.timedelta(days=20),
            structure_id=88,
            structure_name="Private Athanor",
            corporation_id=99,
            corporation_name="B-UH",
            extraction_started_at=base - dt.timedelta(days=20),
            pop_at=base,
            closes_at=base + dt.timedelta(days=3),
            due_at=base + dt.timedelta(days=3),
            tax_rate=Decimal(5),
            allowed_recipients=[
                {"kind": "CHARACTER", "id": 955001, "name": "Fifty5D"}
            ],
            status=TaxPeriod.Status.DUE,
            gross_value=Decimal(1000999),
            tax_due=Decimal(10049),
        )
        TaxAssessment.objects.create(
            period=cls.period,
            billing_key=f"user:{cls.one.pk}",
            auth_user=cls.one,
            display_name="VISIBLE-ACCOUNT",
            gross_value=Decimal(1000),
            tax_due=Decimal(50),
            due_at=cls.period.due_at,
        )
        TaxAssessment.objects.create(
            period=cls.period,
            billing_key=f"user:{cls.two.pk}",
            auth_user=cls.two,
            display_name="HIDDEN-ACCOUNT",
            gross_value=Decimal(999999),
            tax_due=Decimal(9999),
            due_at=cls.period.due_at,
        )

    def test_member_dashboard_never_renders_other_accounts(self):
        self.client.force_login(
            self.one, backend="django.contrib.auth.backends.ModelBackend"
        )
        response = self.client.get(reverse("buh_moon_tax:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "VISIBLE-ACCOUNT")
        self.assertNotContains(response, "HIDDEN-ACCOUNT")
        self.assertNotContains(response, "999,999")
        period_url = reverse("buh_moon_tax:period", args=(self.period.pk,))
        profile_url = reverse("buh_moon_tax:member_detail", args=(self.one.pk,))
        self.assertContains(response, f'data-row-url="{period_url}"')
        self.assertContains(response, f'data-row-url="{profile_url}"')
        self.assertContains(response, f'href="{profile_url}"')
        self.assertContains(
            response,
            'tabindex="0" aria-keyshortcuts="Enter Space" '
            'aria-label="View extraction records for Private Athanor"',
        )
        self.assertContains(
            response,
            'tabindex="0" aria-keyshortcuts="Enter Space" '
            'aria-label="Open person workspace for VISIBLE-ACCOUNT"',
        )

    def test_empty_dashboard_colspans_match_restricted_columns(self):
        app_permission = Permission.objects.get(
            content_type__app_label="buh_moon_tax",
            codename="view_moon_tax",
        )
        self.two.user_permissions.add(app_permission)
        self.client.force_login(
            self.two, backend="django.contrib.auth.backends.ModelBackend"
        )

        response = self.client.get(reverse("buh_moon_tax:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<td colspan="5">', count=1)
        self.assertContains(response, '<td colspan="4">', count=1)

    def test_member_profile_is_scoped_to_their_own_account(self):
        self.client.force_login(
            self.one, backend="django.contrib.auth.backends.ModelBackend"
        )
        own_profile = reverse("buh_moon_tax:member_detail", args=(self.one.pk,))
        response = self.client.get(own_profile)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PERSON WORKSPACE")
        self.assertContains(response, "Private Athanor")
        self.assertNotContains(response, "HIDDEN-ACCOUNT")
        self.assertNotContains(response, "Save billing mode")
        response = self.client.get(
            reverse("buh_moon_tax:member_detail", args=(self.two.pk,))
        )
        self.assertEqual(response.status_code, 404)

    def test_member_period_totals_and_recipients_are_scoped(self):
        self.client.force_login(
            self.one, backend="django.contrib.auth.backends.ModelBackend"
        )
        response = self.client.get(
            reverse("buh_moon_tax:period", args=(self.period.pk,))
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "VISIBLE-ACCOUNT")
        self.assertContains(response, "Fifty5D")
        self.assertNotContains(response, "HIDDEN-ACCOUNT")
        self.assertNotContains(response, "999,999")

    def test_outstanding_kpis_sum_positive_balances_per_bill(self):
        visible = TaxAssessment.objects.get(display_name="VISIBLE-ACCOUNT")
        visible.approved_paid = Decimal("100.00")
        visible.save(update_fields=("approved_paid",))
        TaxAssessment.objects.create(
            period=self.period,
            billing_key="character:90009999",
            auth_user=self.one,
            character_id=90009999,
            display_name="SECOND-VISIBLE-BILL",
            combined_characters=False,
            gross_value=Decimal("800.00"),
            calculated_tax_due=Decimal("40.00"),
            tax_due=Decimal("40.00"),
            due_at=self.period.due_at,
        )
        self.client.force_login(
            self.one, backend="django.contrib.auth.backends.ModelBackend"
        )

        dashboard = self.client.get(reverse("buh_moon_tax:dashboard"))
        period = self.client.get(
            reverse("buh_moon_tax:period", args=(self.period.pk,))
        )

        self.assertEqual(
            dashboard.context["totals"]["outstanding"], Decimal("40.00")
        )
        self.assertEqual(
            dashboard.context["periods"][0].visible_outstanding,
            Decimal("40.00"),
        )
        self.assertEqual(
            period.context["visible_totals"]["outstanding"], Decimal("40.00")
        )

    def test_dashboard_combines_every_extraction_for_each_billing_account(self):
        visible = TaxAssessment.objects.get(display_name="VISIBLE-ACCOUNT")
        visible.approved_paid = Decimal("75.00")
        visible.save(update_fields=("approved_paid",))
        second_pop = self.period.pop_at + dt.timedelta(days=1)
        second_period = TaxPeriod.objects.create(
            source_started_at=second_pop - dt.timedelta(days=20),
            structure_id=89,
            structure_name="Second Athanor",
            corporation_id=99,
            corporation_name="B-UH",
            extraction_started_at=second_pop - dt.timedelta(days=20),
            pop_at=second_pop,
            closes_at=second_pop + dt.timedelta(days=3),
            due_at=second_pop + dt.timedelta(days=3),
            tax_rate=Decimal(5),
            status=TaxPeriod.Status.DUE,
        )
        TaxAssessment.objects.create(
            period=second_period,
            billing_key=f"user:{self.one.pk}",
            auth_user=self.one,
            display_name="VISIBLE-ACCOUNT",
            gross_value=Decimal("2000.00"),
            calculated_tax_due=Decimal("80.00"),
            tax_due=Decimal("80.00"),
            approved_paid=Decimal("20.00"),
            due_at=second_period.due_at,
        )
        self.client.force_login(
            self.one, backend="django.contrib.auth.backends.ModelBackend"
        )

        response = self.client.get(reverse("buh_moon_tax:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["billing_accounts"]), 1)
        account = response.context["billing_accounts"][0]
        self.assertEqual(account["bills"], 2)
        self.assertEqual(account["gross"], Decimal("3000.00"))
        self.assertEqual(account["tax"], Decimal("130.00"))
        self.assertEqual(account["paid"], Decimal("95.00"))
        self.assertEqual(account["outstanding"], Decimal("60.00"))
        self.assertEqual(account["billing_label"], "Combined account")
        self.assertEqual(
            account["profile_url"],
            reverse("buh_moon_tax:member_detail", args=(self.one.pk,)),
        )
        self.assertContains(response, "Totals by billing account")
        self.assertContains(response, 'data-sort-value="3000"')
        self.assertNotContains(response, "HIDDEN-ACCOUNT")

    def test_user_without_app_permission_is_denied(self):
        self.client.force_login(
            self.two, backend="django.contrib.auth.backends.ModelBackend"
        )
        response = self.client.get(reverse("buh_moon_tax:dashboard"))
        self.assertEqual(response.status_code, 403)


class PaymentReviewWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.director = get_user_model().objects.create_user("payments-director")
        cls.member = get_user_model().objects.create_user("payment-member")
        cls.outsider = get_user_model().objects.create_user("payment-outsider")
        for offset, user in enumerate((cls.director, cls.member, cls.outsider)):
            character = EveCharacter.objects.create(
                character_id=91000000 + offset,
                character_name=f"Payment Pilot {offset}",
                corporation_id=98000001,
                corporation_name="Bureau of Unified Harvesting",
            )
            user.profile.main_character = character
            user.profile.save(update_fields=["main_character"])
        permissions = Permission.objects.filter(
            content_type__app_label="buh_moon_tax",
            codename__in=(
                "view_moon_tax",
                "view_payment_evidence",
                "review_payments",
            ),
        )
        cls.director.user_permissions.add(*permissions)
        cls.member.user_permissions.add(
            *permissions.exclude(codename="review_payments")
        )
        TaxConfiguration.objects.create(
            singleton_id=1,
            notify_member_payment_decision=False,
        )
        cls.member_payment = PaymentCandidate.objects.create(
            source=PaymentCandidate.Source.WALLET,
            source_key="wallet:review-member",
            payer_character_id=91000001,
            payer_character_name="Payment Alt Alpha",
            auth_user=cls.member,
            recipient_id=955001,
            recipient_name="Fifty5D",
            amount=Decimal("125.50"),
            occurred_at=now() - dt.timedelta(days=2),
            reference="moon tax alpha",
        )
        cls.outsider_payment = PaymentCandidate.objects.create(
            source=PaymentCandidate.Source.CONTRACT,
            source_key="contract:review-outsider",
            payer_character_id=91000002,
            payer_character_name="Payment Alt Bravo",
            auth_user=cls.outsider,
            recipient_id=98000001,
            recipient_name="Bureau of Unified Harvesting",
            amount=Decimal("900.00"),
            occurred_at=now() - dt.timedelta(days=1),
            reference="contract beta",
        )

    def login_director(self):
        self.client.force_login(
            self.director,
            backend="django.contrib.auth.backends.ModelBackend",
        )

    def test_payment_page_uses_buttons_and_member_search(self):
        self.login_director()
        response = self.client.get(
            reverse("buh_moon_tax:payments"),
            {"status": "ALL", "q": "Payment Pilot 1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Payment Pilot 1")
        self.assertNotContains(response, "Payment Pilot 2")
        self.assertContains(response, 'value="OLDEST_CARRY"')
        self.assertContains(response, 'id="tax-bulk-form"')
        self.assertContains(
            response,
            reverse(
                "buh_moon_tax:decide_payment_direct",
                args=(self.member_payment.pk,),
            ),
        )
        self.assertContains(response, "data-payment-decision-form")
        self.assertContains(response, "data-select-visible")
        self.assertNotContains(response, "data-payment-drag")
        self.assertNotContains(response, "data-decision-dock")
        self.assertNotContains(response, "data-drop-decision")
        self.assertNotContains(response, 'draggable="true"')
        self.assertNotContains(response, "Choose director decision")
        self.assertEqual(response.context["summary"]["count"], 1)

    def test_bulk_ignore_changes_every_selected_payment(self):
        self.login_director()
        response = self.client.post(
            reverse("buh_moon_tax:bulk_decide_payments"),
            {
                "payment_ids": [
                    self.member_payment.pk,
                    self.outsider_payment.pk,
                ],
                "decision": "IGNORE",
                "note": "Batch review",
            },
        )
        self.assertRedirects(response, reverse("buh_moon_tax:payments"))
        payments = PaymentCandidate.objects.filter(
            pk__in=(self.member_payment.pk, self.outsider_payment.pk)
        )
        self.assertEqual(
            set(payments.values_list("status", flat=True)),
            {PaymentCandidate.Status.IGNORED},
        )
        self.assertEqual(
            PaymentAllocation.objects.filter(payment__in=payments).count(),
            2,
        )

    def test_row_accept_has_a_dedicated_working_endpoint(self):
        self.login_director()
        response = self.client.post(
            reverse(
                "buh_moon_tax:decide_payment_direct",
                args=(self.member_payment.pk,),
            ),
            {"decision": "OLDEST_CARRY", "note": "One-click row review"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.member_payment.refresh_from_db()
        self.assertEqual(self.member_payment.status, PaymentCandidate.Status.OVERAGE)

    def test_row_accept_works_as_a_native_form_without_javascript(self):
        self.login_director()
        response = self.client.post(
            reverse(
                "buh_moon_tax:decide_payment_direct",
                args=(self.member_payment.pk,),
            ),
            {"decision": "OLDEST_CARRY", "note": "Native form fallback"},
        )
        self.assertRedirects(response, reverse("buh_moon_tax:payments"))
        self.member_payment.refresh_from_db()
        self.assertEqual(self.member_payment.status, PaymentCandidate.Status.OVERAGE)

    @patch(
        "buh_moon_tax.views.decide_payment",
        side_effect=ValueError(
            "A system-excluded exempt payment cannot be manually reclassified."
        ),
    )
    def test_row_decision_reports_concurrent_exemption_as_conflict(self, mocked):
        self.login_director()
        response = self.client.post(
            reverse(
                "buh_moon_tax:decide_payment_direct",
                args=(self.member_payment.pk,),
            ),
            {"decision": "OLDEST_CARRY", "note": "Concurrent review"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["ok"])
        self.assertIn("exempt payment", response.json()["message"])
        mocked.assert_called_once()

    def test_member_cannot_use_director_bulk_action(self):
        self.client.force_login(
            self.member,
            backend="django.contrib.auth.backends.ModelBackend",
        )
        response = self.client.post(
            reverse("buh_moon_tax:bulk_decide_payments"),
            {
                "payment_ids": [self.member_payment.pk],
                "decision": "IGNORE",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.member_payment.refresh_from_db()
        self.assertEqual(
            self.member_payment.status,
            PaymentCandidate.Status.PENDING,
        )

    def test_system_exempt_payment_cannot_be_manually_reclassified(self):
        self.login_director()
        self.member_payment.status = PaymentCandidate.Status.EXEMPT
        self.member_payment.review_note = "[AUTO-EXEMPT] Test"
        self.member_payment.save(update_fields=("status", "review_note"))
        self.client.post(
            reverse("buh_moon_tax:decide_payment"),
            {
                "payment_id": self.member_payment.pk,
                "decision": "OLDEST_CARRY",
            },
        )
        self.member_payment.refresh_from_db()
        self.assertEqual(
            self.member_payment.status,
            PaymentCandidate.Status.EXEMPT,
        )


class MemberPreviewAndPolicyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.director = get_user_model().objects.create_user("preview-director")
        cls.member = get_user_model().objects.create_user("preview-member")
        cls.other = get_user_model().objects.create_user("preview-other")
        for offset, user in enumerate((cls.director, cls.member, cls.other), start=1):
            character = EveCharacter.objects.create(
                character_id=93000000 + offset,
                character_name=f"Preview Pilot {offset}",
                corporation_id=98000001,
                corporation_name="Bureau of Unified Harvesting",
            )
            user.profile.main_character = character
            user.profile.save(update_fields=["main_character"])
        director_permissions = Permission.objects.filter(
            content_type__app_label="buh_moon_tax",
            codename__in=(
                "view_moon_tax",
                "view_all_tax",
                "view_mining_values",
                "view_mining_quantities",
                "preview_member_view",
                "manage_tax_policy",
                "manage_bill_adjustments",
                "view_unlinked_miners",
                "run_tax_audit",
            ),
        )
        cls.director.user_permissions.add(*director_permissions)
        member_permissions = Permission.objects.filter(
            content_type__app_label="buh_moon_tax",
            codename__in=("view_moon_tax", "view_own_tax", "view_mining_values"),
        )
        cls.member.user_permissions.add(*member_permissions)
        cls.config = TaxConfiguration.objects.create(
            singleton_id=1,
            default_tax_rate=Decimal("5.00"),
            notify_member_payment_decision=False,
        )
        cls.recipient = PaymentRecipient.objects.create(
            kind=PaymentRecipient.Kind.CHARACTER,
            entity_id=93000099,
            name="Member Investor",
        )
        base = now() - dt.timedelta(days=5)
        cls.period = TaxPeriod.objects.create(
            source_started_at=base - dt.timedelta(days=10),
            structure_id=445566,
            structure_name="Investor Athanor",
            corporation_id=98000001,
            corporation_name="B-UH",
            extraction_started_at=base - dt.timedelta(days=10),
            pop_at=base,
            closes_at=base + dt.timedelta(days=3),
            due_at=base + dt.timedelta(days=3),
            tax_rate=Decimal("5.00"),
            allowed_recipients=[
                {"kind": "CORPORATION", "id": 98000001, "name": "B-UH"}
            ],
            status=TaxPeriod.Status.DUE,
        )
        TaxAssessment.objects.create(
            period=cls.period,
            billing_key=f"user:{cls.member.pk}",
            auth_user=cls.member,
            display_name="MEMBER-PREVIEW-BILL",
            gross_value=Decimal(1000),
            calculated_tax_due=Decimal(50),
            tax_due=Decimal(50),
            due_at=cls.period.due_at,
        )
        TaxAssessment.objects.create(
            period=cls.period,
            billing_key="character:998877",
            character_id=998877,
            display_name="UNLINKED-PILOT",
            combined_characters=False,
            gross_value=Decimal(300),
            calculated_tax_due=Decimal(15),
            tax_due=Decimal(15),
            due_at=cls.period.due_at,
        )
        TaxAssessment.objects.create(
            period=cls.period,
            billing_key=f"user:{cls.other.pk}",
            auth_user=cls.other,
            display_name="OTHER-HIDDEN-BILL",
            gross_value=Decimal(5000),
            calculated_tax_due=Decimal(250),
            tax_due=Decimal(250),
            due_at=cls.period.due_at,
        )

    def login(self):
        self.client.force_login(
            self.director,
            backend="django.contrib.auth.backends.ModelBackend",
        )

    def test_member_preview_uses_real_scope_and_blocks_mutation(self):
        self.login()
        response = self.client.post(
            reverse("buh_moon_tax:start_member_preview"),
            {"user_id": self.member.pk},
        )
        self.assertRedirects(response, reverse("buh_moon_tax:dashboard"))
        response = self.client.get(reverse("buh_moon_tax:dashboard"))
        self.assertContains(response, "Member POV · read only")
        self.assertContains(response, "MEMBER-PREVIEW-BILL")
        self.assertNotContains(response, "OTHER-HIDDEN-BILL")
        response = self.client.post(reverse("buh_moon_tax:run_audit"))
        self.assertEqual(response.status_code, 403)

    def test_per_athanor_policy_preserves_existing_period_snapshot(self):
        self.login()
        workspace = self.client.get(reverse("buh_moon_tax:policy"))
        self.assertContains(workspace, "Investor Athanor")
        self.assertContains(workspace, "Member Investor")
        self.assertContains(workspace, "data-recipient-search")
        response = self.client.post(
            reverse("buh_moon_tax:save_structure_policy"),
            {
                "structure_id": self.period.structure_id,
                "tax_rate": "7.5",
                "payment_recipients": [self.recipient.pk],
                "notes": "Member-funded Athanor",
            },
        )
        self.assertRedirects(response, reverse("buh_moon_tax:policy"))
        policy = StructureTaxPolicy.objects.get(structure_id=self.period.structure_id)
        self.assertEqual(policy.tax_rate, Decimal("7.5000"))
        self.assertEqual(list(policy.payment_recipients.all()), [self.recipient])
        self.period.refresh_from_db()
        self.assertEqual(self.period.tax_rate, Decimal("5.0000"))
        self.assertEqual(self.period.allowed_recipients[0]["name"], "B-UH")

    def test_all_six_tax_tables_have_typed_canonical_sort_keys(self):
        payment_permission = Permission.objects.get(
            content_type__app_label="buh_moon_tax",
            codename="view_payment_evidence",
        )
        self.director.user_permissions.add(payment_permission)
        for cache_name in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
            self.director.__dict__.pop(cache_name, None)

        audit = AuditRun.objects.create(
            trigger=AuditRun.Trigger.MANUAL,
            status=AuditRun.Status.COMPLETE,
        )
        MiningLine.objects.create(
            period=self.period,
            ledger_day=self.period.pop_at.date(),
            character_id=93000999,
            character_name="Zulu Sort Pilot",
            recorded_corporation_id=98000999,
            recorded_corporation_name="Zulu Sort Corporation",
            raw_type_id=17464,
            raw_type_name="Golden Omber",
            compressed_type_id=28421,
            compressed_type_name="Compressed Golden Omber",
            raw_quantity=1234567,
            compressed_quantity=Decimal("12345.67890000"),
            compressed_unit_price=Decimal("42.12500000"),
            gross_value=Decimal("520123.45"),
            tax_rate=Decimal("5.0000"),
            tax_due=Decimal("26006.17"),
            pricing_complete=False,
            last_audit_run=audit,
        )

        self.login()
        dashboard = self.client.get(reverse("buh_moon_tax:dashboard"))
        period = self.client.get(
            reverse("buh_moon_tax:period", args=(self.period.pk,))
        )
        profile = self.client.get(
            reverse("buh_moon_tax:member_detail", args=(self.member.pk,))
        )

        self.assertContains(dashboard, "data-sortable-table", count=3)
        self.assertContains(period, "data-sortable-table", count=2)
        self.assertContains(profile, "data-sortable-table", count=2)

        mining_headers = (
            ("date", "Date"),
            ("text", "Character"),
            ("text", "Recorded corporation"),
            ("text", "Raw ore"),
            ("number", "Quantity"),
            ("number", "Compressed equivalent"),
            ("number", "Jita buy/unit"),
            ("number", "Value"),
            ("number", "Tax"),
            ("text", "Flags"),
        )
        rendered_period = period.content.decode()
        for sort_type, label in mining_headers:
            self.assertRegex(
                rendered_period,
                rf'<th[^>]*data-sort-type="{sort_type}"[^>]*>{re.escape(label)}</th>',
            )

        canonical_keys = (
            self.period.pop_at.date().isoformat(),
            "Zulu Sort Pilot",
            "Zulu Sort Corporation",
            "Golden Omber",
            "1234567",
            "12345.67890000",
            "42.12500000",
            "520123.45",
            "26006.17",
            "Price review",
        )
        for key in canonical_keys:
            self.assertContains(period, f'data-sort-value="{key}"')

    def test_director_can_waive_a_small_balance_from_period_workspace(self):
        self.login()
        bill = TaxAssessment.objects.get(display_name="MEMBER-PREVIEW-BILL")
        workspace = self.client.get(
            reverse("buh_moon_tax:period", args=(self.period.pk,))
        )
        self.assertContains(workspace, "Waive 50")
        self.assertContains(workspace, "data-sortable-table", count=2)
        self.assertContains(workspace, "Compressed equivalent")
        self.assertContains(workspace, 'data-sort-type="number"')
        self.assertContains(
            workspace,
            f'data-bill-toggle="bill-actions-{bill.pk}" '
            f'aria-controls="bill-actions-{bill.pk}" aria-expanded="false"',
        )
        self.assertContains(
            workspace,
            f'id="bill-actions-{bill.pk}" data-sort-child hidden><td colspan="8">',
        )
        response = self.client.post(
            reverse("buh_moon_tax:adjust_assessment", args=(bill.pk,)),
            {
                "action": "WAIVE_BALANCE",
                "reason": "Small Jita price variance",
            },
        )
        self.assertRedirects(
            response,
            reverse("buh_moon_tax:period", args=(self.period.pk,)),
        )
        bill.refresh_from_db()
        self.assertEqual(bill.tax_due, Decimal(0))
        self.assertEqual(bill.status, TaxAssessment.Status.WAIVED)

    def test_overview_rows_open_period_and_person_and_can_waive(self):
        self.login()
        bill = TaxAssessment.objects.get(display_name="MEMBER-PREVIEW-BILL")
        dashboard_url = reverse("buh_moon_tax:dashboard")
        profile_url = reverse("buh_moon_tax:member_detail", args=(self.member.pk,))
        period_url = reverse("buh_moon_tax:period", args=(self.period.pk,))
        response = self.client.get(dashboard_url)
        self.assertContains(response, f'data-row-url="{period_url}"')
        self.assertContains(response, f'data-row-url="{profile_url}"')
        self.assertContains(response, "data-sortable-table", count=3)
        self.assertContains(response, f'id="overview-bill-actions-{bill.pk}"')
        self.assertContains(response, "Waive / adjust")
        self.assertContains(
            response,
            f'data-bill-toggle="overview-bill-actions-{bill.pk}" '
            f'aria-controls="overview-bill-actions-{bill.pk}" aria-expanded="false"',
        )
        self.assertContains(
            response,
            f'id="overview-bill-actions-{bill.pk}" data-sort-child hidden>'
            '<td colspan="9">',
        )

        response = self.client.post(
            reverse("buh_moon_tax:adjust_assessment", args=(bill.pk,)),
            {
                "action": "WAIVE_BALANCE",
                "reason": "Small overview variance",
                "return_to": dashboard_url,
            },
        )
        self.assertRedirects(response, dashboard_url)
        bill.refresh_from_db()
        self.assertEqual(bill.tax_due, Decimal(0))
        self.assertEqual(bill.calculated_tax_due, Decimal(50))
        self.assertEqual(bill.status, TaxAssessment.Status.WAIVED)

    def test_adjustment_and_source_colspans_without_value_permission(self):
        value_permission = Permission.objects.get(
            content_type__app_label="buh_moon_tax",
            codename="view_mining_values",
        )
        self.director.user_permissions.remove(value_permission)
        self.login()
        bill = TaxAssessment.objects.get(display_name="MEMBER-PREVIEW-BILL")

        dashboard = self.client.get(reverse("buh_moon_tax:dashboard"))
        self.assertContains(
            dashboard,
            f'id="overview-bill-actions-{bill.pk}" data-sort-child hidden>'
            '<td colspan="5">',
        )

        period = self.client.get(
            reverse("buh_moon_tax:period", args=(self.period.pk,))
        )
        self.assertContains(
            period,
            f'id="bill-actions-{bill.pk}" data-sort-child hidden><td colspan="4">',
        )
        self.assertContains(period, '<td colspan="7">', count=1)

    def test_person_profile_has_scoped_history_and_account_controls(self):
        self.login()
        profile_url = reverse("buh_moon_tax:member_detail", args=(self.member.pk,))
        response = self.client.get(profile_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Preview Pilot 2")
        self.assertContains(response, "MEMBER-PREVIEW-BILL")
        self.assertNotContains(response, "OTHER-HIDDEN-BILL")
        self.assertNotContains(response, "UNLINKED-PILOT")
        self.assertContains(response, "Save billing mode")
        self.assertContains(response, "View as member")
        self.assertContains(response, "Waive / adjust")

        response = self.client.post(
            reverse("buh_moon_tax:save_billing_preference", args=(self.member.pk,)),
            {"mode": "SEPARATE"},
        )
        self.assertRedirects(response, profile_url)
        preference = BillingPreference.objects.get(user=self.member)
        self.assertFalse(preference.combine_characters)
        self.assertEqual(preference.updated_by, self.director)

    def test_person_profile_can_return_billing_to_the_site_default(self):
        BillingPreference.objects.create(
            user=self.member,
            combine_characters=False,
            updated_by=self.director,
        )
        self.login()
        profile_url = reverse("buh_moon_tax:member_detail", args=(self.member.pk,))

        response = self.client.post(
            reverse("buh_moon_tax:save_billing_preference", args=(self.member.pk,)),
            {"mode": "DEFAULT"},
        )

        self.assertRedirects(response, profile_url)
        self.assertFalse(BillingPreference.objects.filter(user=self.member).exists())
        profile = self.client.get(profile_url)
        self.assertEqual(profile.context["billing_mode"], "DEFAULT")
        self.assertTrue(profile.context["combine_characters"])
        self.assertContains(
            profile,
            'name="mode" value="DEFAULT" checked',
            html=False,
        )
        self.assertContains(profile, "Site default")

    def test_person_profile_labels_linked_character_exemption_states(self):
        today = now().date()
        member_character = self.member.profile.main_character
        other_character = self.other.profile.main_character
        TaxExemption.objects.create(
            auth_user=self.member,
            effective_from=today - dt.timedelta(days=2),
            effective_until=today,
            reason="ACCOUNT-CURRENT",
        )
        TaxExemption.objects.create(
            character_id=member_character.character_id,
            effective_from=today + dt.timedelta(days=1),
            reason="CHARACTER-FUTURE",
        )
        TaxExemption.objects.create(
            character_id=member_character.character_id,
            character_name="Stored old label",
            effective_from=today - dt.timedelta(days=10),
            effective_until=today - dt.timedelta(days=1),
            reason="CHARACTER-EXPIRED",
        )
        TaxExemption.objects.create(
            character_id=member_character.character_id,
            effective_from=today - dt.timedelta(days=1),
            reason="CHARACTER-INACTIVE",
            active=False,
        )
        TaxExemption.objects.create(
            character_id=other_character.character_id,
            character_name=other_character.character_name,
            effective_from=today,
            reason="OTHER-CHARACTER-HIDDEN",
        )
        self.login()

        response = self.client.get(
            reverse("buh_moon_tax:member_detail", args=(self.member.pk,))
        )

        exemptions = {
            item.reason: item for item in response.context["exemptions"]
        }
        self.assertEqual(
            exemptions["ACCOUNT-CURRENT"].effective_state,
            "current",
        )
        self.assertEqual(
            exemptions["ACCOUNT-CURRENT"].target_label,
            "Entire Auth account · Preview Pilot 2",
        )
        self.assertEqual(
            exemptions["CHARACTER-FUTURE"].effective_state,
            "future",
        )
        self.assertEqual(
            exemptions["CHARACTER-EXPIRED"].effective_state,
            "expired",
        )
        self.assertEqual(
            exemptions["CHARACTER-INACTIVE"].effective_state,
            "inactive",
        )
        self.assertEqual(
            exemptions["CHARACTER-EXPIRED"].target_label,
            "Character · Preview Pilot 2",
        )
        self.assertNotIn("OTHER-CHARACTER-HIDDEN", exemptions)
        for label in ("Current", "Future", "Expired", "Inactive"):
            self.assertContains(response, f">{label}<", html=False)

    def test_person_profile_paginates_bills_but_keeps_full_history_totals(self):
        base = now() - dt.timedelta(days=10)
        for index in range(26):
            pop_at = base - dt.timedelta(days=index)
            period = TaxPeriod.objects.create(
                source_started_at=pop_at - dt.timedelta(days=20),
                structure_id=700000 + index,
                structure_name=f"Paged Athanor {index}",
                corporation_id=98000001,
                corporation_name="B-UH",
                extraction_started_at=pop_at - dt.timedelta(days=20),
                pop_at=pop_at,
                closes_at=pop_at + dt.timedelta(days=3),
                due_at=pop_at + dt.timedelta(days=3),
                tax_rate=Decimal("5.00"),
                status=TaxPeriod.Status.DUE,
            )
            TaxAssessment.objects.create(
                period=period,
                billing_key=f"user:{self.member.pk}:page:{index}",
                auth_user=self.member,
                character_id=self.member.profile.main_character.character_id,
                display_name=f"PAGINATED-BILL-{index}",
                combined_characters=False,
                gross_value=Decimal(10),
                calculated_tax_due=Decimal(2),
                tax_due=Decimal(2),
                due_at=period.due_at,
            )
        self.login()
        profile_url = reverse("buh_moon_tax:member_detail", args=(self.member.pk,))

        first = self.client.get(profile_url)

        self.assertEqual(first.context["bill_page_obj"].paginator.count, 27)
        self.assertEqual(len(first.context["assessments"]), 25)
        self.assertEqual(first.context["totals"]["bills"], 27)
        self.assertEqual(first.context["totals"]["gross"], Decimal(1260))
        self.assertEqual(first.context["totals"]["outstanding"], Decimal(102))
        self.assertContains(first, "Page 1 of 2")
        self.assertNotContains(first, "PAGINATED-BILL-25")

        second = self.client.get(f"{profile_url}?bills_page=2")

        self.assertEqual(len(second.context["assessments"]), 2)
        self.assertEqual(second.context["totals"], first.context["totals"])
        self.assertContains(second, "PAGINATED-BILL-25")
        self.assertContains(second, "Showing 26–27 of 27 bills")

    def test_unlinked_ledger_row_opens_unlinked_character_profile(self):
        self.login()
        profile_url = reverse("buh_moon_tax:character_detail", args=(998877,))
        response = self.client.get(reverse("buh_moon_tax:dashboard"))
        self.assertContains(response, f'data-row-url="{profile_url}"')
        profile = self.client.get(profile_url)
        self.assertEqual(profile.status_code, 200)
        self.assertContains(profile, "UNLINKED-PILOT")
        self.assertNotContains(profile, "OTHER-HIDDEN-BILL")

    def test_bill_adjustment_permission_cannot_escape_visible_account_scope(self):
        permission = Permission.objects.get(
            content_type__app_label="buh_moon_tax",
            codename="manage_bill_adjustments",
        )
        self.member.user_permissions.add(permission)
        hidden_bill = TaxAssessment.objects.get(display_name="OTHER-HIDDEN-BILL")
        self.client.force_login(
            self.member,
            backend="django.contrib.auth.backends.ModelBackend",
        )

        response = self.client.post(
            reverse("buh_moon_tax:adjust_assessment", args=(hidden_bill.pk,)),
            {
                "action": "WAIVE_BALANCE",
                "reason": "Must not cross account scope",
            },
        )

        self.assertEqual(response.status_code, 404)
        hidden_bill.refresh_from_db()
        self.assertEqual(hidden_bill.tax_due, Decimal(250))
