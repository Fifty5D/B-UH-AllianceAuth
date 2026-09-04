"""Verify that the v0.3.3 accounting and operations evidence survived migration."""

from decimal import Decimal

from django.conf import settings

from buh_moon_tax.models import AuditRun, TaxAssessment, TaxConfiguration, TaxPeriod
from buh_structure_ops.models import StructurePreference, TrackedCorporation


database = settings.DATABASES["default"]
if (
    database.get("ENGINE") != "django.db.backends.mysql"
    or database.get("HOST") != "db"
    or database.get("NAME") not in {"buh_test", "buh_restore"}
):
    raise RuntimeError("Refusing to verify outside the reserved disposable databases")

configuration = TaxConfiguration.objects.get(singleton_id=1)
assert configuration.default_tax_rate == Decimal("5.0000")
assert configuration.combine_characters_by_default is True
assert configuration.enforcement_enabled is False

corporation = TrackedCorporation.objects.get(corporation_id=98000001)
assert corporation.notes == "buh-upgrade-v0.3.3"
assert corporation.is_enabled is True

preference = StructurePreference.objects.get(structure_id=120000001)
assert preference.target_fuel_days == 45
assert preference.notes == "buh-upgrade-v0.3.3"

audit = AuditRun.objects.get(pk=9900001)
assert audit.summary == {"fixture": "buh-upgrade-v0.3.3"}

period = TaxPeriod.objects.get(
    structure_id=120000001,
    source_started_at__isnull=False,
)
assert period.tax_rate == Decimal("5.0000")
assert period.tax_due == Decimal("12500000.00")
assert period.approved_paid == Decimal("2000000.00")
assert period.outstanding == Decimal("10500000.00")
assert period.allowed_recipients == [
    {"kind": "CORPORATION", "entity_id": 98000001}
]

assessment = TaxAssessment.objects.get(
    period=period,
    billing_key="character:99000999",
)
assert assessment.character_id == 99000999
assert assessment.display_name == "Upgrade Sentinel Miner"
assert assessment.tax_due == Decimal("12500000.00")
assert assessment.approved_paid == Decimal("2000000.00")
assert assessment.outstanding == Decimal("10500000.00")
print(f"Verified v0.3.3 upgrade evidence in {database['NAME']}.")
