"""Fuel bands, state deduplication, and Auth notification tests."""

import datetime as dt
from unittest.mock import patch

from app_utils.testdata_factories import UserFactory
from django.test import TestCase
from django.utils.timezone import now
from structures.tests.testdata.factories import StructureFactory

from buh_structure_ops.models import AlertEvent, TrackedCorporation
from buh_structure_ops.services import (
    _fuel_status,
    _state_alert,
    dispatch_pending_notifications,
    evaluate_fuel_alerts,
)


class TestFuelBands(TestCase):
    def test_requested_red_boundary(self):
        self.assertEqual(_fuel_status(6.99), "critical")
        self.assertEqual(_fuel_status(7), "warning")
        self.assertEqual(_fuel_status(13.99), "warning")
        self.assertEqual(_fuel_status(14), "healthy")
        self.assertEqual(_fuel_status(None), "unknown")

    def test_missing_fuel_expiry_is_safe_and_creates_no_alert(self):
        structure = StructureFactory(fuel_expires_at=None)
        TrackedCorporation.objects.create(
            corporation_id=structure.owner_id,
            corporation_name=structure.owner.corporation.corporation_name,
        )
        self.assertEqual(evaluate_fuel_alerts(), 0)
        self.assertFalse(AlertEvent.objects.exists())


class TestStateAlerts(TestCase):
    def test_state_alert_deduplicates_resolves_and_reactivates(self):
        kwargs = {
            "event_key": "sync:test:1",
            "kind": AlertEvent.Kind.SYNC,
            "severity": AlertEvent.Severity.DANGER,
            "title": "Sync failed",
            "message": "Token problem",
            "corporation_id": 1,
        }
        _state_alert(active=True, **kwargs)
        _state_alert(active=True, **kwargs)
        self.assertEqual(AlertEvent.objects.count(), 1)
        event = AlertEvent.objects.get()
        self.assertEqual(event.occurrence_count, 1)
        _state_alert(active=False, **kwargs)
        event.refresh_from_db()
        self.assertFalse(event.is_active)
        _state_alert(active=True, **kwargs)
        event.refresh_from_db()
        self.assertTrue(event.is_active)
        self.assertEqual(event.occurrence_count, 2)
        self.assertIsNone(event.notification_sent_at)

    @patch("buh_structure_ops.services.notify")
    def test_pending_alert_notifies_manager_once(self, mock_notify):
        manager = UserFactory(permissions=["buh_structure_ops.manage_structure_ops"])
        event = AlertEvent.objects.create(
            event_key="fuel:critical:100",
            kind=AlertEvent.Kind.FUEL,
            severity=AlertEvent.Severity.DANGER,
            title="Fuel critical",
            message="Six days remain",
            occurred_at=now() - dt.timedelta(minutes=1),
        )
        self.assertEqual(dispatch_pending_notifications(), 1)
        mock_notify.assert_called_once_with(
            user=manager,
            title="Fuel critical",
            message="Six days remain",
            level="danger",
        )
        event.refresh_from_db()
        self.assertIsNotNone(event.notification_sent_at)
        self.assertEqual(dispatch_pending_notifications(), 0)
