import uuid
from unittest.mock import patch

from app_utils.testdata_factories import UserMainFactory
from django.test import TestCase
from django.urls import reverse

from buh_vps_health.models import AlertState, HealthConfiguration, MetricSnapshot
from buh_vps_health.services import (
    capture_snapshot,
    evaluate_alerts,
    record_agent_recovery,
)

HOST = {
    "cpu_percent": 42.5,
    "cpu_count": 4,
    "load_1": 0.4,
    "load_5": 0.3,
    "load_15": 0.2,
    "memory_percent": 55,
    "memory_used_bytes": 550,
    "memory_total_bytes": 1000,
    "swap_percent": 0,
    "disk_percent": 30,
    "disk_used_bytes": 300,
    "disk_total_bytes": 1000,
    "network_rx_bytes_per_second": 12,
    "network_tx_bytes_per_second": 8,
    "uptime_seconds": 900,
}
METRICS = {
    "host": HOST,
    "docker": {"total": 2, "healthy": 2, "unhealthy": 0, "containers": []},
    "workers": {"desired": 2, "online": 2},
}
WORKLOAD = {
    "available": True,
    "queued": 7,
    "active_total": 1,
    "reserved_total": 0,
    "scheduled_total": 2,
    "nodes": [],
    "error": "",
}


def filesystem(identifier, used=300, *, available=True):
    return {
        "id": identifier,
        "label": "System disk" if identifier == "system" else "Auth data",
        "mountpoint": "/" if identifier == "system" else "/srv/buh-data",
        "available": available,
        "status": "mounted" if available else "not_mounted",
        "total_bytes": 1000 if available else None,
        "used_bytes": used if available else None,
        "free_bytes": 1000 - used if available else None,
        "available_bytes": 1000 - used if available else None,
        "percent": used / 10 if available else None,
    }


class DashboardPermissionTests(TestCase):
    def setUp(self):
        self.outsider = UserMainFactory()
        self.viewer = UserMainFactory(
            permissions=[
                "buh_vps_health.view_vps_health",
                "buh_vps_health.view_host_metrics",
            ]
        )
        self.operator = UserMainFactory(
            permissions=[
                "buh_vps_health.view_vps_health",
                "buh_vps_health.view_host_metrics",
                "buh_vps_health.view_container_metrics",
                "buh_vps_health.view_worker_workload",
                "buh_vps_health.view_task_queue",
                "buh_vps_health.view_action_history",
                "buh_vps_health.check_updates",
                "buh_vps_health.view_update_results",
                "buh_vps_health.restart_auth_services",
                "buh_vps_health.scale_workers",
            ]
        )

    def test_outsider_is_denied(self):
        self.client.force_login(self.outsider)
        self.assertEqual(
            self.client.get(reverse("buh_vps_health:dashboard")).status_code, 403
        )

    def test_host_only_viewer_does_not_receive_docker_or_controls(self):
        self.client.force_login(self.viewer)
        with patch("buh_vps_health.views.get_metrics", return_value=METRICS):
            response = self.client.get(reverse("buh_vps_health:live_metrics"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["host"]["cpu_percent"], 42.5)
        self.assertEqual(payload["docker"], {})
        self.assertEqual(payload["actions"], [])
        page = self.client.get(reverse("buh_vps_health:dashboard"))
        self.assertNotContains(page, "Restart Auth services")
        self.assertNotContains(page, "Container health")

    def test_queue_permission_does_not_leak_worker_or_task_names(self):
        queue_viewer = UserMainFactory(
            permissions=[
                "buh_vps_health.view_vps_health",
                "buh_vps_health.view_task_queue",
            ]
        )
        private_workload = {**WORKLOAD, "nodes": [{"name": "private-node"}]}
        self.client.force_login(queue_viewer)
        with (
            patch("buh_vps_health.views.get_metrics", return_value=METRICS),
            patch(
                "buh_vps_health.views.get_worker_workload",
                return_value=private_workload,
            ),
        ):
            response = self.client.get(reverse("buh_vps_health:live_metrics"))
        self.assertEqual(response.json()["workload"], {"queued": 7})

    def test_filesystem_metrics_keep_host_permissions(self):
        metrics = {
            **METRICS,
            "host": {**HOST, "filesystems": [filesystem("system"), filesystem("data")]},
        }
        self.client.force_login(self.viewer)
        with patch("buh_vps_health.views.get_metrics", return_value=metrics):
            self.assertEqual(
                self.client.get(reverse("buh_vps_health:live_metrics")).json()["host"][
                    "filesystems"
                ],
                metrics["host"]["filesystems"],
            )
        queue_viewer = UserMainFactory(
            permissions=["buh_vps_health.view_vps_health", "buh_vps_health.view_task_queue"]
        )
        self.client.force_login(queue_viewer)
        with (
            patch("buh_vps_health.views.get_metrics", return_value=metrics),
            patch("buh_vps_health.views.get_worker_workload", return_value=WORKLOAD),
        ):
            self.assertEqual(
                self.client.get(reverse("buh_vps_health:live_metrics")).json()["host"], {}
            )
        self.assertEqual(
            self.client.get(reverse("buh_vps_health:history")).status_code, 403
        )

    def test_history_preserves_root_series_and_unknown_legacy_data(self):
        old = MetricSnapshot.objects.create(disk_percent=30, disk_total_bytes=1000)
        current = capture_snapshot(
            {
                **METRICS,
                "host": {
                    **HOST,
                    "filesystems": [filesystem("system"), filesystem("data", 900)],
                },
            },
            WORKLOAD,
        )
        self.client.force_login(self.viewer)
        points = self.client.get(reverse("buh_vps_health:history")).json()["points"]
        self.assertEqual(
            [item["disk_percent"] for item in points],
            [old.disk_percent, current.disk_percent],
        )
        self.assertEqual(points[0]["filesystems"], [])
        self.assertEqual(points[1]["filesystems"][1]["percent"], 90)

    @patch("buh_vps_health.views.submit_action")
    def test_restart_requires_phrase_and_is_audited(self, submit):
        submit.return_value = {
            "action": {
                "id": str(uuid.uuid4()),
                "message": "accepted",
            }
        }
        self.client.force_login(self.operator)
        url = reverse("buh_vps_health:restart_auth")
        self.assertEqual(self.client.post(url, {"confirmation": "yes"}).status_code, 400)
        response = self.client.post(url, {"confirmation": "RESTART AUTH"})
        self.assertEqual(response.status_code, 202)
        submit.assert_called_once()
        self.assertEqual(response.json()["action"]["action"], "RESTART_AUTH")

    @patch("buh_vps_health.views.submit_action")
    def test_worker_count_respects_configured_range(self, submit):
        config = HealthConfiguration.get_solo()
        config.worker_minimum = 2
        config.worker_maximum = 4
        config.save()
        self.client.force_login(self.operator)
        url = reverse("buh_vps_health:set_workers")
        self.assertEqual(self.client.post(url, {"count": 1}).status_code, 400)
        submit.return_value = {"action": {"id": str(uuid.uuid4()), "message": "accepted"}}
        self.assertEqual(self.client.post(url, {"count": 4}).status_code, 202)

    @patch("buh_vps_health.views.submit_action")
    def test_update_scan_requires_permission_and_is_audited(self, submit):
        submit.return_value = {"action": {"id": str(uuid.uuid4()), "message": "accepted"}}
        url = reverse("buh_vps_health:check_updates")
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.operator)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["action"]["action"], "CHECK_UPDATES")
        submit.assert_called_once()


class SnapshotAndAlertTests(TestCase):
    def capture_filesystems(self, system_used=300, data_used=300, *, available=True):
        host = {
            **HOST,
            "disk_percent": system_used / 10,
            "disk_used_bytes": system_used,
            "filesystems": [
                filesystem("system", system_used),
                filesystem("data", data_used, available=available),
            ],
        }
        return capture_snapshot({**METRICS, "host": host}, WORKLOAD)

    def sustained_config(self):
        config = HealthConfiguration.get_solo()
        config.alert_sustain_minutes = 2
        config.snapshot_interval_minutes = 1
        config.warning_disk_percent = 85
        config.save()
        return config

    def test_first_healthy_capture_does_not_create_recovery_notification(self):
        snapshot = capture_snapshot(METRICS, WORKLOAD)
        self.assertEqual(snapshot.queued_tasks, 7)
        self.assertEqual(snapshot.filesystems, [])
        self.assertIsNone(record_agent_recovery())
        evaluate_alerts(snapshot)
        self.assertFalse(
            AlertState.objects.filter(notification_sent_at__isnull=True).exists()
        )

    def test_sustained_cpu_threshold_activates_alert(self):
        config = HealthConfiguration.get_solo()
        config.warning_cpu_percent = 80
        config.alert_sustain_minutes = 2
        config.snapshot_interval_minutes = 1
        config.save()
        for _ in range(2):
            MetricSnapshot.objects.create(cpu_percent=91, cpu_count=4)
        evaluate_alerts(MetricSnapshot.objects.first(), config)
        self.assertTrue(AlertState.objects.get(key="cpu").active)

    def test_first_healthy_filesystems_do_not_create_recovery_notifications(self):
        evaluate_alerts(self.capture_filesystems())
        self.assertFalse(
            AlertState.objects.filter(notification_sent_at__isnull=True).exists()
        )

    def test_data_only_disk_pressure_does_not_change_root_history_or_alert(self):
        config = self.sustained_config()
        for _ in range(2):
            snapshot = self.capture_filesystems(data_used=950)
            evaluate_alerts(snapshot, config)
        self.assertEqual(snapshot.disk_percent, 30)
        self.assertEqual(snapshot.disk_total_bytes, HOST["disk_total_bytes"])
        self.assertTrue(AlertState.objects.get(key="disk:data").active)
        self.assertFalse(AlertState.objects.get(key="disk").active)

    def test_root_pressure_is_not_diluted_by_free_data_space(self):
        config = self.sustained_config()
        for _ in range(2):
            evaluate_alerts(self.capture_filesystems(system_used=950, data_used=10), config)
        self.assertTrue(AlertState.objects.get(key="disk").active)
        self.assertFalse(AlertState.objects.get(key="disk:data").active)

    def test_alternating_full_filesystems_are_not_one_sustained_alert(self):
        config = self.sustained_config()
        evaluate_alerts(self.capture_filesystems(system_used=950, data_used=10), config)
        evaluate_alerts(self.capture_filesystems(system_used=10, data_used=950), config)
        self.assertFalse(AlertState.objects.get(key="disk").active)
        self.assertFalse(AlertState.objects.get(key="disk:data").active)

    def test_unavailable_and_old_agent_samples_cannot_falsely_recover_data_pressure(self):
        config = self.sustained_config()
        for _ in range(2):
            evaluate_alerts(self.capture_filesystems(data_used=950), config)
        evaluate_alerts(self.capture_filesystems(available=False), config)
        self.assertTrue(AlertState.objects.get(key="disk:data").active)
        self.assertTrue(AlertState.objects.get(key="disk:data:unavailable").active)
        evaluate_alerts(capture_snapshot(METRICS, WORKLOAD), config)
        self.assertTrue(AlertState.objects.get(key="disk:data").active)
        self.assertTrue(AlertState.objects.get(key="disk:data:unavailable").active)
        evaluate_alerts(self.capture_filesystems(data_used=200), config)
        self.assertFalse(AlertState.objects.get(key="disk:data").active)
        self.assertFalse(AlertState.objects.get(key="disk:data:unavailable").active)

    def test_malformed_data_reading_is_unknown_rather_than_zero_percent(self):
        for overrides in ({"used_bytes": "invalid"}, {"status": []}):
            with self.subTest(overrides=overrides):
                data = {**filesystem("data"), **overrides}
                snapshot = capture_snapshot(
                    {
                        **METRICS,
                        "host": {**HOST, "filesystems": [filesystem("system"), data]},
                    },
                    WORKLOAD,
                )
                self.assertFalse(snapshot.filesystems[1]["available"])
                self.assertIsNone(snapshot.filesystems[1]["percent"])
                self.assertEqual(snapshot.disk_percent, HOST["disk_percent"])
