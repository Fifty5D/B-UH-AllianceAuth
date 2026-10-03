"""Host selection/traffic/report boundaries use synthetic metadata only."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from ops.incidents import run_structures_pilot as pilot


class PilotBoundaryTests(unittest.TestCase):
    def report(self):
        return {"database": {"structures": [{
            "owner_pk": 20, "enabled": True, "configured_characters": [{
                "owner_character_id": 30, "enabled": False,
                "auth_link_exists": True, "disabled_for_no_valid_token": True,
                "stored_corporation_matches_owner": True,
                "identity": {"character_name": "Synthetic Pilot", "ownership_id": 40,
                             "user_id": 50, "user_active": True,
                             "user_has_structures_owner_permission": True,
                             "character_id": 60000,
                             "tokens": [{"id": 70, "user_id": 50, "character_id": 60000,
                                         "has_refresh_credential": True, "missing_scopes": [],
                                         "owner_identity_matches_auth_link": True}]},
            }],
        }]}}

    def test_retained_evidence_reuses_only_known_report_fields_and_original_observation_time(self):
        report = self.report()
        report.update({
            "diagnostic_source_commit": pilot.QUALIFIED_REPORT_COMMIT,
            "finished_at": "2026-10-01T12:00:00+00:00", "attempt_id": "gh-111111-1",
            "journal": {"state": "migrated"}, "raw_env": "synthetic-secret-must-not-export",
        })
        report["database"].update({
            "read_only": True, "scan_complete": True, "memberaudit": [{"character_id": 60000}],
            "memberaudit_required_scopes": ["synthetic-scope"],
            "password": "synthetic-password-must-not-export",
        })
        evidence = pilot.retained_incident_evidence(report, "a" * 64)
        self.assertTrue(evidence["read_only"])
        self.assertEqual(evidence["source"], "retained_sanitized_incident_report")
        self.assertEqual(evidence["observed_at"], report["finished_at"])
        self.assertEqual(evidence["database"]["memberaudit"], report["database"]["memberaudit"])
        self.assertEqual(evidence["report_sha256"], "a" * 64)
        self.assertEqual(evidence["recovery"]["journal"], report["journal"])
        encoded = json.dumps(evidence)
        self.assertNotIn("synthetic-secret-must-not-export", encoded)
        self.assertNotIn("synthetic-password-must-not-export", encoded)

    def test_exact_single_existing_token_selection(self):
        selected = pilot.select_pilot(self.report(), "Synthetic Pilot")
        self.assertEqual(selected, {"owner_pk": 20, "owner_character_pk": 30,
                                    "auth_link_pk": 40, "character_id": 60000, "token_pk": 70})

    def test_ambiguous_tokens_are_never_guessed(self):
        report = self.report()
        tokens = report["database"]["structures"][0]["configured_characters"][0]["identity"]["tokens"]
        tokens.append({**tokens[0], "id": 71})
        with self.assertRaises(pilot.PilotStop):
            pilot.select_pilot(report, "Synthetic Pilot")

    def test_different_disabled_reason_or_identity_does_not_qualify(self):
        for changed in ("disabled_for_no_valid_token", "auth_link_exists"):
            report = self.report()
            report["database"]["structures"][0]["configured_characters"][0][changed] = False
            with self.assertRaises(pilot.PilotStop):
                pilot.select_pilot(report, "Synthetic Pilot")
        report = self.report()
        report["database"]["structures"][0]["configured_characters"][0]["identity"]["tokens"][0]["owner_identity_matches_auth_link"] = None
        with self.assertRaises(pilot.PilotStop):
            pilot.select_pilot(report, "Synthetic Pilot")

    def container(self, name="previous-web"):
        return {"Id": "a" * 64, "Name": "/" + name, "Image": "sha256:" + "b" * 64,
                "State": {"Running": True},
                "Config": {"Labels": {
                    "com.b-uh.platform.version": "0.8.2",
                    "com.b-uh.platform.release": pilot.OLD_RELEASE,
                    "com.b-uh.platform.source": pilot.OLD_SOURCE,
                    "com.b-uh.platform.manifest": pilot.OLD_MANIFEST,
                }},
                "NetworkSettings": {"Networks": {"test": {"Aliases": [name], "IPAddress": "192.0.2.10"}}}}

    def test_previous_image_requires_all_provenance_fields(self):
        container = self.container()
        pilot.assert_previous_image(container, container["Image"])
        for field in ("com.b-uh.platform.version", "com.b-uh.platform.release",
                      "com.b-uh.platform.source", "com.b-uh.platform.manifest"):
            altered = self.container()
            altered["Config"]["Labels"][field] = "different"
            with self.assertRaises(pilot.PilotStop):
                pilot.assert_previous_image(altered, container["Image"])

    def test_current_unexpected_or_ambiguous_result_is_rejected(self):
        good = {"schema_version": 1, "recovered": False}
        wrapped = "BUH_STRUCTURES_RECOVERY_BEGIN\n" + json.dumps(good) + "\nBUH_STRUCTURES_RECOVERY_END"
        self.assertEqual(pilot.parse_result(wrapped), good)
        for bad in (wrapped + wrapped, "truncated", wrapped.replace('"recovered": false', '"recovered": null')):
            with self.assertRaises(pilot.PilotStop):
                pilot.parse_result(bad)

    def test_traffic_is_bound_to_old_images_before_any_database_operation(self):
        config = SimpleNamespace(state_dir=Path("/synthetic/state"), app_dir=Path("/synthetic/app"),
                                 backup_dir=Path("/synthetic/backup"), auth_services=("web",), local_settings=Path("local.py"),
                                 custom_dockerfile=Path("Dockerfile"), gunicorn_service="web",
                                 gunicorn_port=8000, nginx_upstream_file=Path("upstream.conf"),
                                 nginx_upstream_container_file="/upstream.conf")
        attempt = "gh-111111-1"
        old = self.container()
        candidate = self.container("candidate-web")
        candidate["Id"] = "c" * 64
        candidate["Image"] = "sha256:" + "d" * 64
        candidate["Config"]["Labels"]["com.b-uh.platform.version"] = "0.8.3"
        host = MagicMock()
        host._running_service_containers.return_value = [old["Id"]]
        host._run.side_effect = lambda args, **kwargs: (
            old["Id"] + "\n" + candidate["Id"] if args[1] == "ps"
            else json.dumps([old, candidate])
        )
        hold = {"attempt_id": attempt, "status": "active", "phase": "candidate-slot-start-1",
                "previous_images": {"web": [old["Image"], "synthetic:previous"]},
                "auth_replica_counts": {"web": 1}}
        journal = {"attempt_id": attempt, "state": "migrated", "result": "failed"}
        current = {"platform_version": "0.8.2", "release_commit": pilot.OLD_RELEASE}
        route = b"upstream auth { server previous-web:8000; }\n"
        host._proxy_exec.return_value = route.decode()

        def private(path, maximum):
            if path.name == "active-recovery.json":
                return json.dumps(hold).encode()
            if path.parent.name == "attempts":
                return json.dumps(journal).encode()
            if path.name == "CURRENT.json":
                return json.dumps(current).encode()
            if path.name == "upstream.conf":
                return route
            return b"synthetic unchanged configuration"

        with (patch.object(pilot, "host_resources", return_value={
                  "cpu_count": 4, "host_load_average": [0.1, 0.1, 0.1],
                  "memory_total_bytes": 4 * 1024 ** 3, "memory_available_bytes": 2 * 1024 ** 3}),
              patch.object(pilot, "private_bytes", side_effect=private),
              patch.object(pilot.os, "statvfs", return_value=SimpleNamespace(
                  f_bavail=100 * 1024 ** 3, f_frsize=1, f_blocks=200 * 1024 ** 3))):
            result = pilot.verify_host(host, config, attempt)
            self.assertTrue(result["traffic_only_verified_previous_image"])
            self.assertEqual(result["active_upstream_container_ids"], [old["Id"]])
            host._public_smoke_checks.assert_called_once()
            route = b"upstream auth { server candidate-web:8000; }\n"
            host._proxy_exec.return_value = route.decode()
            with self.assertRaises(pilot.PilotStop):
                pilot.verify_host(host, config, attempt)
        host._manage_live.assert_not_called()


    def test_resource_guard_stops_low_memory_or_sustained_load(self):
        healthy = {"cpu_count": 4, "host_load_average": [0.1, 0.1, 0.1],
                   "memory_total_bytes": 4 * 1024 ** 3, "memory_available_bytes": 2 * 1024 ** 3}
        pilot.guard_resources(healthy)
        with self.assertRaises(pilot.PilotStop):
            pilot.guard_resources({**healthy, "memory_available_bytes": 128 * 1024 ** 2})
        with self.assertRaises(pilot.PilotStop):
            pilot.guard_resources({**healthy, "host_load_average": [0.1, 7.0, 0.1]})

    def test_snapshot_roster_is_only_qualified_outage_selection(self):
        self.assertEqual(pilot.snapshot_targets(self.report()), [
            pilot.select_pilot(self.report(), "Synthetic Pilot")
        ])
        report = self.report()
        report["database"]["structures"][0]["configured_characters"][0]["disabled_for_no_valid_token"] = False
        with self.assertRaises(pilot.PilotStop):
            pilot.snapshot_targets(report)

    def test_mode_defaults_to_report_and_launcher_is_explicitly_bounded(self):
        source = Path(__file__).with_name("run-structures-pilot.ps1").read_text(encoding="utf-8")
        self.assertIn("$Mode = 'report'", source)
        self.assertIn("timeout=600", source)
        self.assertIn("timeout 660s", source)
        self.assertIn("Get-FileHash -Algorithm SHA256", source)
        self.assertNotIn("refresh_token =", source)
        self.assertNotIn("recover_incomplete_plan", source)


if __name__ == "__main__":
    unittest.main()
