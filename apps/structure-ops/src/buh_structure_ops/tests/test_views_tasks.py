"""View authorization, manager actions, and source task queue tests."""

from unittest.mock import patch

from app_utils.testdata_factories import UserMainFactory
from django.test import TestCase
from django.urls import reverse
from django.utils.timezone import now

from buh_structure_ops.models import AlertEvent, StructurePreference


class TestDashboardViews(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.viewer = UserMainFactory(
            permissions=["buh_structure_ops.view_structure_ops"]
        )
        cls.manager = UserMainFactory(
            permissions=["buh_structure_ops.manage_structure_ops"]
        )
        cls.outsider = UserMainFactory()

    def test_outsider_is_denied(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("buh_structure_ops:dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_viewer_dashboard_renders(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("buh_structure_ops:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Structure Operations")
        self.assertContains(response, "Launch checklist incomplete")

    def test_viewer_cannot_refresh(self):
        self.client.force_login(self.viewer)
        response = self.client.post(reverse("buh_structure_ops:refresh_now"))
        self.assertEqual(response.status_code, 403)

    @patch("buh_structure_ops.views.cache.add", return_value=True)
    @patch("buh_structure_ops.views.queue_source_refreshes.delay")
    def test_manager_can_refresh(self, mock_delay, _mock_cache_add):
        self.client.force_login(self.manager)
        response = self.client.post(reverse("buh_structure_ops:refresh_now"))
        self.assertEqual(response.status_code, 302)
        mock_delay.assert_called_once_with()

    def test_manager_can_acknowledge_and_set_target(self):
        alert = AlertEvent.objects.create(
            event_key="test:alert",
            kind=AlertEvent.Kind.ATTACK,
            severity=AlertEvent.Severity.DANGER,
            title="Attack",
            message="Under attack",
            occurred_at=now(),
        )
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("buh_structure_ops:acknowledge_alert", args=(alert.pk,))
        )
        self.assertEqual(response.status_code, 302)
        alert.refresh_from_db()
        self.assertEqual(alert.acknowledged_by, self.manager)
        response = self.client.post(
            reverse("buh_structure_ops:update_preference", args=(123456789,)),
            {"target_fuel_days": 30, "notes": "Top off before Friday"},
        )
        self.assertEqual(response.status_code, 302)
        preference = StructurePreference.objects.get(structure_id=123456789)
        self.assertEqual(preference.target_fuel_days, 30)
        self.assertEqual(preference.updated_by, self.manager)


class TestTaskQueue(TestCase):
    @patch("buh_structure_ops.tasks.capture_and_evaluate.apply_async")
    @patch("moonmining.tasks.run_regular_updates.delay")
    @patch("structures.tasks.fetch_all_notifications.delay")
    @patch("structures.tasks.update_all_structures.delay")
    def test_manual_refresh_queues_every_source(
        self, structure_delay, notification_delay, moon_delay, followup
    ):
        from buh_structure_ops.tasks import queue_source_refreshes

        queue_source_refreshes.run()
        structure_delay.assert_called_once_with()
        notification_delay.assert_called_once_with()
        moon_delay.assert_called_once_with()
        followup.assert_called_once_with(countdown=90)
