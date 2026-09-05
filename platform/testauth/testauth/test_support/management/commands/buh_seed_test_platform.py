"""Seed deterministic synthetic identities and Moon Tax records for CI/browser tests."""

import datetime as dt
from decimal import Decimal

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from buh_structure_ops.console_theme import sync_console_theme
from buh_moon_tax.models import (
    AssessmentAdjustment,
    AuditRun,
    BillingPreference,
    MiningLine,
    PaymentAllocation,
    PaymentCandidate,
    TaxAssessment,
    TaxConfiguration,
    TaxExemption,
    TaxPeriod,
)

SYNTHETIC_CHARACTER_IDS = (
    99000001,
    99000002,
    99000003,
    99000004,
    99000005,
    99000012,
    99000015,
    99000999,
)


class Command(BaseCommand):
    help = "Create deterministic fake B-UH records for disposable tests."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true")

    def handle(self, *args, **options):
        if not getattr(settings, "BUH_TEST_SUPPORT_ENABLED", False):
            raise CommandError("Test support is disabled in this settings profile.")
        if not options["reset"]:
            raise CommandError("Synthetic reseeding requires an explicit --reset flag.")

        database = settings.DATABASES["default"]
        if (
            database.get("ENGINE") != "django.db.backends.mysql"
            or database.get("HOST") != "db"
            or database.get("NAME") != "buh_test"
        ):
            raise CommandError(
                "Refusing to reset outside the reserved disposable db/buh_test database."
            )

        # Rebuild only the reserved synthetic namespace on every run. This makes the
        # command safe to retry after a partially completed CI job.
        self._clear_synthetic_data()
        sync_console_theme()

        director = self._user("test-director", 99000001, "Director Pilot")
        member = self._user(
            "test-member",
            99000002,
            "Member Main",
            alts=((99000012, "Member Alt"),),
        )
        restricted = self._user("test-restricted", 99000003, "Restricted Pilot")
        self._user("test-no-access", 99000004, "No Access Pilot")
        separate = self._user(
            "test-separate",
            99000005,
            "Separate Main",
            alts=((99000015, "Separate Alt"),),
        )

        all_tax_permissions = Permission.objects.filter(
            content_type__app_label="buh_moon_tax"
        )
        director.user_permissions.add(*all_tax_permissions)
        director.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="buh_structure_ops")
        )
        director.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="buh_mining_analytics")
        )
        member_permissions = all_tax_permissions.filter(
            codename__in=(
                "view_moon_tax",
                "view_own_tax",
                "view_mining_quantities",
                "view_mining_values",
                "view_payment_evidence",
            )
        )
        member.user_permissions.add(*member_permissions)
        separate.user_permissions.add(*member_permissions)
        restricted.user_permissions.add(
            *all_tax_permissions.filter(codename="view_moon_tax")
        )
        director.is_staff = True
        director.save(update_fields=("is_staff",))

        TaxConfiguration.objects.update_or_create(
            singleton_id=1,
            defaults={
                "default_tax_rate": Decimal("5.0000"),
                "combine_characters_by_default": True,
                "payment_character_id": 99000001,
                "payment_character_name": "Director Pilot",
                "payment_corporation_id": 98000001,
                "payment_corporation_name": "Bureau of Unified Harvesting",
            },
        )
        BillingPreference.objects.update_or_create(
            user=member,
            defaults={"combine_characters": True, "updated_by": director},
        )
        BillingPreference.objects.update_or_create(
            user=separate,
            defaults={"combine_characters": False, "updated_by": director},
        )

        fixed = timezone.make_aware(dt.datetime(2026, 8, 30, 12, 0))
        audit = AuditRun.objects.create(
            trigger=AuditRun.Trigger.INSTALL,
            status=AuditRun.Status.COMPLETE,
            requested_by=director,
            started_at=fixed,
            finished_at=fixed + dt.timedelta(minutes=1),
            summary={"fixture": True},
        )
        first = self._period(
            fixed,
            structure_id=120000001,
            structure_name="Test Ainsan Athanor",
            moon_name="Ainsan VI - 1",
        )
        second = self._period(
            fixed - dt.timedelta(days=1),
            structure_id=120000002,
            structure_name="Test Talidal Athanor",
            moon_name="Talidal IV - 2",
        )

        first_bill = TaxAssessment.objects.create(
            period=first,
            billing_key=f"user:{member.pk}",
            auth_user=member,
            display_name="Member Main",
            combined_characters=True,
            gross_value=Decimal("217302154.00"),
            calculated_tax_due=Decimal("10865108.00"),
            tax_due=Decimal("10865108.00"),
            due_at=first.due_at,
            status=TaxAssessment.Status.DUE,
        )
        second_bill = TaxAssessment.objects.create(
            period=second,
            billing_key=f"user:{member.pk}",
            auth_user=member,
            display_name="Member Main",
            combined_characters=True,
            gross_value=Decimal("300266999.00"),
            calculated_tax_due=Decimal("15013350.00"),
            tax_due=Decimal("15013350.00"),
            approved_paid=Decimal("12000000.00"),
            due_at=second.due_at,
            status=TaxAssessment.Status.PARTIAL,
        )
        TaxAssessment.objects.create(
            period=first,
            billing_key="character:99000999",
            character_id=99000999,
            display_name="Unlinked Test Miner",
            combined_characters=False,
            gross_value=Decimal("118568243.00"),
            calculated_tax_due=Decimal("5928412.00"),
            tax_due=Decimal("5928412.00"),
            due_at=first.due_at,
            status=TaxAssessment.Status.DUE,
        )
        TaxAssessment.objects.create(
            period=first,
            billing_key="character:99000015",
            auth_user=separate,
            character_id=99000015,
            display_name="Separate Alt",
            combined_characters=False,
            gross_value=Decimal("62646895.00"),
            calculated_tax_due=Decimal("3132345.00"),
            tax_due=Decimal("3132345.00"),
            due_at=first.due_at,
            status=TaxAssessment.Status.DUE,
        )
        TaxAssessment.objects.create(
            period=second,
            billing_key="character:99000005",
            auth_user=separate,
            character_id=99000005,
            display_name="Separate Main",
            combined_characters=False,
            gross_value=Decimal("8914946.00"),
            calculated_tax_due=Decimal("445747.00"),
            tax_due=Decimal("445747.00"),
            due_at=second.due_at,
            status=TaxAssessment.Status.DUE,
        )

        MiningLine.objects.create(
            period=first,
            ledger_day=fixed.date(),
            character_id=99000002,
            character_name="Member Main",
            recorded_corporation_id=98000001,
            recorded_corporation_name="Bureau of Unified Harvesting",
            auth_user=member,
            raw_type_id=46676,
            raw_type_name="Glistening Bitumens",
            compressed_type_id=62454,
            compressed_type_name="Compressed Glistening Bitumens",
            raw_quantity=123456,
            compressed_quantity=Decimal("123456"),
            compressed_unit_price=Decimal("1760.00"),
            gross_value=Decimal("217282560.00"),
            tax_rate=Decimal("5.0000"),
            tax_due=Decimal("10864128.00"),
            pricing_complete=True,
            last_audit_run=audit,
        )
        MiningLine.objects.create(
            period=first,
            ledger_day=fixed.date() + dt.timedelta(days=1),
            character_id=99000012,
            character_name="Member Alt",
            recorded_corporation_id=98000001,
            recorded_corporation_name="Bureau of Unified Harvesting",
            auth_user=member,
            raw_type_id=46677,
            raw_type_name="Shimmering Bitumens",
            compressed_type_id=62455,
            compressed_type_name="Compressed Shimmering Bitumens",
            raw_quantity=54321,
            compressed_quantity=Decimal("54321"),
            compressed_unit_price=Decimal("900.00"),
            gross_value=Decimal("48888900.00"),
            tax_rate=Decimal("5.0000"),
            tax_due=Decimal("2444445.00"),
            pricing_complete=True,
            last_audit_run=audit,
        )
        MiningLine.objects.create(
            period=second,
            ledger_day=(fixed - dt.timedelta(days=1)).date(),
            character_id=99000015,
            character_name="Separate Alt",
            recorded_corporation_id=98000002,
            recorded_corporation_name="Synthetic Holdings",
            auth_user=separate,
            raw_type_id=46678,
            raw_type_name="Zeolites",
            compressed_type_id=62456,
            compressed_type_name="Compressed Zeolites",
            raw_quantity=9876,
            compressed_quantity=Decimal("9876"),
            compressed_unit_price=Decimal("420.00"),
            gross_value=Decimal("4147920.00"),
            tax_rate=Decimal("5.0000"),
            tax_due=Decimal("207396.00"),
            pricing_complete=True,
            last_audit_run=audit,
        )
        MiningLine.objects.create(
            period=second,
            ledger_day=fixed.date(),
            character_id=99000005,
            character_name="Separate Main",
            recorded_corporation_id=98000001,
            recorded_corporation_name="Bureau of Unified Harvesting",
            auth_user=separate,
            raw_type_id=46679,
            raw_type_name="Sylvite",
            compressed_type_id=62457,
            compressed_type_name="Compressed Sylvite",
            raw_quantity=21226,
            compressed_quantity=Decimal("21226"),
            compressed_unit_price=Decimal("420.00"),
            gross_value=Decimal("8914920.00"),
            tax_rate=Decimal("5.0000"),
            tax_due=Decimal("445746.00"),
            pricing_complete=True,
            last_audit_run=audit,
        )
        prior_payment = PaymentCandidate.objects.create(
            source=PaymentCandidate.Source.WALLET,
            source_key="test:wallet:prior-approved",
            source_id=70000000,
            payer_character_id=99000002,
            payer_character_name="Member Main",
            auth_user=member,
            recipient_id=99000001,
            recipient_name="Director Pilot",
            amount=Decimal("12000000.00"),
            occurred_at=fixed + dt.timedelta(days=2),
            reference="Synthetic previously approved payment",
            evidence={"fixture": True},
            status=PaymentCandidate.Status.APPROVED,
            imported_by_audit=audit,
            reviewed_by=director,
            reviewed_at=fixed + dt.timedelta(days=2, minutes=1),
            review_note="Synthetic prior allocation",
        )
        PaymentAllocation.objects.create(
            payment=prior_payment,
            assessment=second_bill,
            category=PaymentAllocation.Category.TAX,
            amount=Decimal("12000000.00"),
            notes="Synthetic prior allocation",
            created_by=director,
        )
        PaymentCandidate.objects.create(
            source=PaymentCandidate.Source.WALLET,
            source_key="test:wallet:1",
            source_id=70000001,
            payer_character_id=99000002,
            payer_character_name="Member Main",
            auth_user=member,
            recipient_id=99000001,
            recipient_name="Director Pilot",
            amount=Decimal("5000000.00"),
            occurred_at=fixed + dt.timedelta(days=4),
            reference="Synthetic Moon Tax payment",
            evidence={"fixture": True},
            status=PaymentCandidate.Status.PENDING,
            imported_by_audit=audit,
        )
        PaymentCandidate.objects.create(
            source=PaymentCandidate.Source.CONTRACT,
            source_key="test:contract:1",
            source_id=70000002,
            payer_character_id=99000012,
            payer_character_name="Member Alt",
            auth_user=member,
            recipient_id=98000001,
            recipient_name="Bureau of Unified Harvesting",
            amount=Decimal("7500000.00"),
            occurred_at=fixed + dt.timedelta(days=3),
            reference="Synthetic completed ISK contract",
            evidence={"fixture": True, "contract_status": "finished"},
            status=PaymentCandidate.Status.PENDING,
            imported_by_audit=audit,
        )
        adjustment = AssessmentAdjustment.objects.create(
            assessment=first_bill,
            kind=AssessmentAdjustment.Kind.CORRECTION,
            amount=Decimal("-1000.00"),
            reason="Synthetic correction history",
            created_by=director,
        )
        adjustment.reversed_at = fixed + dt.timedelta(days=5)
        adjustment.reversed_by = director
        adjustment.reversal_reason = "Synthetic reversal history"
        adjustment.save(
            update_fields=("reversed_at", "reversed_by", "reversal_reason")
        )
        TaxExemption.objects.create(
            auth_user=restricted,
            effective_from=fixed.date() - dt.timedelta(days=30),
            reason="Synthetic account-wide exemption",
            created_by=director,
        )
        TaxExemption.objects.create(
            character_id=99000015,
            character_name="Separate Alt",
            effective_from=fixed.date() - dt.timedelta(days=7),
            effective_until=fixed.date() + dt.timedelta(days=7),
            reason="Synthetic character-specific exemption",
            created_by=director,
        )

        self.stdout.write(self.style.SUCCESS("Seeded deterministic B-UH test data."))

    @staticmethod
    def _clear_synthetic_data():
        periods = TaxPeriod.objects.filter(structure_name__startswith="Test ")
        assessments = TaxAssessment.objects.filter(period__in=periods)
        PaymentCandidate.objects.filter(source_key__startswith="test:").delete()
        PaymentAllocation.objects.filter(assessment__in=assessments).delete()
        AssessmentAdjustment.objects.filter(assessment__in=assessments).delete()
        MiningLine.objects.filter(period__in=periods).delete()
        periods.delete()
        TaxExemption.objects.filter(reason__startswith="Synthetic").delete()
        AuditRun.objects.filter(summary__fixture=True).delete()
        get_user_model().objects.filter(username__startswith="test-").delete()
        CharacterOwnership.objects.filter(
            character__character_id__in=SYNTHETIC_CHARACTER_IDS
        ).delete()
        EveCharacter.objects.filter(character_id__in=SYNTHETIC_CHARACTER_IDS).delete()

    @staticmethod
    def _user(username, character_id, character_name, *, alts=()):
        user = get_user_model().objects.create_user(username=username, password="test-only")
        character, _ = EveCharacter.objects.update_or_create(
            character_id=character_id,
            defaults={
                "character_name": character_name,
                "corporation_id": 98000001,
                "corporation_name": "Bureau of Unified Harvesting",
            },
        )
        CharacterOwnership.objects.update_or_create(
            character=character,
            defaults={"owner_hash": f"test-owner-{character_id}", "user": user},
        )
        user.profile.main_character = character
        user.profile.save(update_fields=("main_character",))
        for alt_id, alt_name in alts:
            alt, _ = EveCharacter.objects.update_or_create(
                character_id=alt_id,
                defaults={
                    "character_name": alt_name,
                    "corporation_id": 98000001,
                    "corporation_name": "Bureau of Unified Harvesting",
                },
            )
            CharacterOwnership.objects.update_or_create(
                character=alt,
                defaults={"owner_hash": f"test-owner-{alt_id}", "user": user},
            )
        return user

    @staticmethod
    def _period(pop_at, *, structure_id, structure_name, moon_name):
        closes_at = pop_at + dt.timedelta(days=3)
        return TaxPeriod.objects.create(
            source_started_at=pop_at - dt.timedelta(days=28),
            structure_id=structure_id,
            structure_name=structure_name,
            corporation_id=98000001,
            corporation_name="Bureau of Unified Harvesting",
            moon_name=moon_name,
            extraction_started_at=pop_at - dt.timedelta(days=28),
            pop_at=pop_at,
            closes_at=closes_at,
            due_at=closes_at,
            tax_rate=Decimal("5.0000"),
            allowed_recipients=[
                {"kind": "CHARACTER", "id": 99000001, "name": "Director Pilot"}
            ],
            status=TaxPeriod.Status.DUE,
        )
