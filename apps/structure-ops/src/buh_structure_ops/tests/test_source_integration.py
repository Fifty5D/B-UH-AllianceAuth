"""Integration tests against the pinned aa-structures source models."""

import datetime as dt

from django.test import TestCase
from django.utils.timezone import now
from structures.models import StructureItem
from structures.tests.testdata.factories import (
    NotificationStructureLostShieldFactory,
    StructureFactory,
    StructureItemFactory,
    StructureServiceFactory,
)

from buh_structure_ops.models import AlertEvent, TrackedCorporation
from buh_structure_ops.services import build_structure_rows, process_structure_events


class TestStructuresIntegration(TestCase):
    def test_dashboard_row_uses_source_fuel_services_and_fittings(self):
        structure = StructureFactory(fuel_expires_at=now() + dt.timedelta(days=10))
        TrackedCorporation.objects.create(
            corporation_id=structure.owner_id,
            corporation_name=structure.owner.corporation.corporation_name,
        )
        fuel = StructureItemFactory(
            structure=structure,
            location_flag=StructureItem.LocationFlag.STRUCTURE_FUEL,
            quantity=1000,
        )
        fuel.last_updated_at = now()
        fuel.save(update_fields=["last_updated_at"])
        StructureItemFactory(
            structure=structure,
            location_flag="RigSlot0",
            eve_type__name="Standup M-Set Moon Drilling Stability I",
        )
        StructureItemFactory(
            structure=structure,
            location_flag="ServiceSlot0",
            eve_type__name="Standup Moon Drill I",
        )
        StructureServiceFactory(
            structure=structure,
            name="Moon Drilling",
        )

        row = build_structure_rows()[0]
        self.assertEqual(row["fuel_quantity"], 1000)
        self.assertGreater(row["fuel_blocks_per_day"], 0)
        self.assertEqual(row["refill_blocks"], row["fuel_blocks_per_day"] * 30 - 1000)
        self.assertEqual(row["services"], ["Moon Drilling"])
        self.assertEqual(row["rigs"], ["Standup M-Set Moon Drilling Stability I"])
        self.assertEqual(row["fitted_services"], ["Standup Moon Drill I"])

    def test_structure_attack_notification_becomes_auth_alert(self):
        structure = StructureFactory()
        TrackedCorporation.objects.create(
            corporation_id=structure.owner_id,
            corporation_name=structure.owner.corporation.corporation_name,
        )
        source = NotificationStructureLostShieldFactory(
            owner=structure.owner,
            structure=structure,
        )

        self.assertEqual(process_structure_events(), 1)
        alert = AlertEvent.objects.get(source_id=str(source.notification_id))
        self.assertEqual(alert.kind, AlertEvent.Kind.ATTACK)
        self.assertEqual(alert.structure_id, structure.id)
        self.assertIn("lost shields", alert.message.lower())
