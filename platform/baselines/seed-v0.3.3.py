"""Create durable synthetic rows using only the deployed v0.3.3 model contract."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.conf import settings

from buh_moon_tax.models import AuditRun, TaxAssessment, TaxConfiguration, TaxPeriod
from buh_structure_ops.models import StructurePreference, TrackedCorporation


database = settings.DATABASES["default"]
if (
    database.get("ENGINE") != "django.db.backends.mysql"
    or database.get("HOST") != "db"
    or database.get("NAME") != "buh_test"
):
    raise RuntimeError("Refusing to seed outside the reserved disposable baseline database")

fixed = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
TaxConfiguration.objects.update_or_create(
    singleton_id=1,
    defaults={
        "default_tax_rate": Decimal("5.0000"),
        "mining_window_days": 3,
        "audit_interval_hours": 4,
        "combine_characters_by_default": True,
        "payment_character_id": 99000001,
        "payment_character_name": "Upgrade Sentinel",
        "payment_corporation_id": 98000001,
        "payment_corporation_name": "B-UH Synthetic Corporation",
        "enforcement_enabled": False,
    },
)
TrackedCorporation.objects.update_or_create(
    corporation_id=98000001,
    defaults={
        "corporation_name": "B-UH Synthetic Corporation",
        "is_required": True,
        "is_enabled": True,
        "notes": "buh-upgrade-v0.3.3",
    },
)
StructurePreference.objects.update_or_create(
    structure_id=120000001,
    defaults={"target_fuel_days": 45, "notes": "buh-upgrade-v0.3.3"},
)
audit, _ = AuditRun.objects.update_or_create(
    pk=9900001,
    defaults={
        "trigger": AuditRun.Trigger.INSTALL,
        "status": AuditRun.Status.COMPLETE,
        "started_at": fixed,
        "finished_at": fixed + timedelta(minutes=1),
        "summary": {"fixture": "buh-upgrade-v0.3.3"},
    },
)
period, _ = TaxPeriod.objects.update_or_create(
    structure_id=120000001,
    source_started_at=fixed - timedelta(days=5),
    defaults={
        "structure_name": "Upgrade Sentinel Athanor",
        "corporation_id": 98000001,
        "corporation_name": "B-UH Synthetic Corporation",
        "moon_id": 40000001,
        "moon_name": "Upgrade I - Moon 1",
        "extraction_started_at": fixed - timedelta(days=5),
        "pop_at": fixed,
        "closes_at": fixed + timedelta(days=3),
        "due_at": fixed + timedelta(days=3),
        "tax_rate": Decimal("5.0000"),
        "allowed_recipients": [
            {"kind": "CORPORATION", "entity_id": 98000001}
        ],
        "status": TaxPeriod.Status.DUE,
        "gross_value": Decimal("250000000.00"),
        "tax_due": Decimal("12500000.00"),
        "approved_paid": Decimal("2000000.00"),
        "last_audit_run": audit,
    },
)
TaxAssessment.objects.update_or_create(
    period=period,
    billing_key="character:99000999",
    defaults={
        "character_id": 99000999,
        "display_name": "Upgrade Sentinel Miner",
        "combined_characters": False,
        "gross_value": Decimal("250000000.00"),
        "calculated_tax_due": Decimal("12500000.00"),
        "adjustment_total": Decimal("0.00"),
        "tax_due": Decimal("12500000.00"),
        "approved_paid": Decimal("2000000.00"),
        "status": TaxAssessment.Status.PARTIAL,
        "due_at": fixed + timedelta(days=3),
    },
)
print("Seeded synthetic legacy-v1 v0.3.3 upgrade evidence.")
