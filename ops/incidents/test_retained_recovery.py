"""Safety and result evidence for the installed retained-plan recovery bridge."""
from contextlib import redirect_stdout
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from ops.incidents import reconcile_structures_attempt as recovery


class RetainedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="buh-recovery-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.config = SimpleNamespace()
        self.host = SimpleNamespace(config=self.config, backup_path=self.path,
                                    previous_web_slots=["synthetic-previous"],
                                    candidate_web_slots=["synthetic-candidate"])
        self.host.restored_health_checks = MagicMock(return_value=[])
        self.plan = {"attempt_id": "gh-123-1", "phase": "candidate-slot-start-1"}
        self.Host = MagicMock()
        self.Host.load_incomplete_plan.side_effect = [(self.host, self.plan), None]
        self.Host.recover_incomplete_plan.return_value = "synthetic supported recovery success"
        self.pilot = MagicMock()
        self.pilot.verify_host.return_value = {"hold_sha256": "a" * 64}
        self.pilot.latest_report.return_value = (self.path, {"attempt_id": "gh-123-1"}, "b" * 64)
        self.target = {"owner_pk": 8, "owner_character_pk": 9, "character_id": 42, "token_pk": 10}
        self.pilot.select_pilot.return_value = self.target
        self.pilot.snapshot_targets.return_value = [self.target]
        self.healthy = {"read_only": True, "owners": [{
            **self.target, "same_auth_link_pk": True, "same_owner_pk": True, "same_character_id": True,
            "state": {"owner_active": True, "enabled": True, "disabled_for_no_valid_token": False,
                      "owner_is_up": True, "freshness": {"all": True}, "token_identity_matches_auth": True,
                      "token_has_refresh_credential": True, "required_missing_scopes": [],
                      "auth_link_pk": 20, "existing_token_ids": [10, 11]},
        }]}
        for name, value in (("snapshot", self.healthy), ("verify_backup", {"size": 100, "sha256": "c" * 64}),
                            ("verify_after", {"platform_version": "0.8.2"}), ("require_backup_unchanged", None)):
            p = patch.object(recovery, name, return_value=value)
            mocked = p.start()
            self.addCleanup(p.stop)
            setattr(self, name, mocked)

    def run_recovery(self, recover=True):
        return recovery.reconcile(self.config, self.Host, self.pilot, "# synthetic source",
                                  "gh-123-1", "a" * 64, "Synthetic Pilot", recover)

    def test_readonly_mode_never_calls_supported_recovery_or_writes_receipt(self):
        report = self.run_recovery(False)
        self.assertTrue(report["scan_complete"])
        self.assertTrue(report["read_only"])
        self.assertTrue(report["ready_for_supported_recovery"])
        self.Host.recover_incomplete_plan.assert_not_called()
        self.verify_after.assert_not_called()
        self.assertFalse((self.path / "STRUCTURES-INCIDENT-RECOVERY.json").exists())

    def test_success_requires_existing_api_postchecks_and_preserves_inventory_and_backup(self):
        report = self.run_recovery()
        self.assertTrue(report["scan_complete"])
        self.assertTrue(report["recovery_completion_is_proven"])
        self.assertTrue(report["token_and_auth_inventory_preserved"])
        self.assertTrue(report["database_backup_preserved"])
        self.Host.recover_incomplete_plan.assert_called_once_with(self.config)
        self.verify_after.assert_called_once()
        self.require_backup_unchanged.assert_called_once()
        receipt = json.loads((self.path / "STRUCTURES-INCIDENT-RECOVERY.json").read_text())
        self.assertTrue(receipt["recovery_completion_is_proven"])
        self.assertEqual(receipt["before_owners"], receipt["after_owners"])
        self.assertFalse(report["deployment_attempted"])
        self.assertFalse(report["memberaudit_mutation_attempted"])

    def test_current_forwarding_failure_stops_before_any_cleanup(self):
        self.healthy["owners"][0]["state"]["freshness"]["all"] = False
        report = self.run_recovery()
        self.assertFalse(report["scan_complete"])
        self.assertEqual(report["guard_reason"], "active_owner_current_health_not_proven")
        self.assertFalse(report["supported_recovery_attempted"])
        self.verify_backup.assert_not_called()
        self.Host.recover_incomplete_plan.assert_not_called()

    def test_roster_link_scope_disabled_or_identity_change_remains_fail_closed(self):
        changes = [
            ("same_auth_link_pk", False, False), ("owner_is_up", False, True),
            ("disabled_for_no_valid_token", True, True), ("required_missing_scopes", ["synthetic-scope"], True),
            ("token_identity_matches_auth", False, True),
        ]
        for key, value, in_state in changes:
            with self.subTest(key=key):
                row = json.loads(json.dumps(self.healthy))
                holder = row["owners"][0]["state"] if in_state else row["owners"][0]
                holder[key] = value
                self.snapshot.return_value = row
                report = self.run_recovery()
                self.assertFalse(report["scan_complete"])
                self.Host.recover_incomplete_plan.assert_not_called()
                self.Host.load_incomplete_plan.side_effect = [(self.host, self.plan), None]
        self.snapshot.return_value = {"read_only": True, "owners": []}
        report = self.run_recovery()
        self.assertEqual(report["guard_reason"], "incident_owner_roster_changed")

    def test_different_attempt_and_digest_are_never_cleaned(self):
        self.plan["attempt_id"] = "gh-999-1"
        report = self.run_recovery()
        self.assertEqual(report["guard_reason"], "retained_attempt_changed")
        self.Host.recover_incomplete_plan.assert_not_called()
        self.plan["attempt_id"] = "gh-123-1"
        self.Host.load_incomplete_plan.side_effect = [(self.host, self.plan), None]
        self.pilot.verify_host.return_value = {"hold_sha256": "f" * 64}
        report = self.run_recovery()
        self.assertEqual(report["guard_reason"], "retained_hold_digest_changed")
        self.snapshot.assert_not_called()

    def test_missing_active_plan_stops_idempotently_without_repeat_cleanup(self):
        self.Host.load_incomplete_plan.side_effect = [None]
        report = self.run_recovery()
        self.assertFalse(report["scan_complete"])
        self.assertEqual(report["guard_reason"], "no_active_incident_plan_do_not_repeat_cleanup")
        self.Host.recover_incomplete_plan.assert_not_called()

    def test_unverified_database_backup_blocks_cleanup(self):
        self.verify_backup.side_effect = recovery.RecoveryGate("retained_database_backup_hash_mismatch")
        report = self.run_recovery()
        self.assertFalse(report["scan_complete"])
        self.Host.recover_incomplete_plan.assert_not_called()
        self.assertEqual(report["failure_phase"], "verify_retained_database_backup")

    def test_backend_failure_is_not_retried_and_never_exports_error_messages(self):
        self.Host.recover_incomplete_plan.side_effect = RuntimeError("synthetic-secret-never-export")
        report = self.run_recovery()
        self.assertFalse(report["scan_complete"])
        self.assertTrue(report["supported_recovery_attempted"])
        self.assertFalse(report["supported_recovery_completed"])
        self.assertEqual(report["failure_phase"], "supported_recovery")
        self.Host.recover_incomplete_plan.assert_called_once()
        self.assertNotIn("synthetic-secret-never-export", json.dumps(report))
        self.verify_after.assert_not_called()

    def test_failed_recovery_collects_all_existing_readonly_probes_without_retry(self):
        fail = MagicMock(side_effect=RuntimeError("synthetic-secret-never-export"))
        succeed = MagicMock()
        self.host.restored_health_checks.return_value = [("django", fail), ("public-http", succeed)]
        self.Host.recover_incomplete_plan.side_effect = RuntimeError("synthetic backend failure")
        report = self.run_recovery()
        self.assertEqual([row["passed"] for row in report["read_only_failure_checks"]], [False, True])
        self.assertNotIn("synthetic-secret-never-export", json.dumps(report))
        self.assertNotIn("synthetic backend failure", json.dumps(report))
        self.Host.recover_incomplete_plan.assert_called_once()
        succeed.assert_called_once()

    def test_uncleared_plan_never_reports_completion(self):
        self.Host.load_incomplete_plan.side_effect = [(self.host, self.plan), (self.host, self.plan)]
        report = self.run_recovery()
        self.assertFalse(report["scan_complete"])
        self.assertEqual(report["guard_reason"], "active_incident_plan_not_retired")
        self.assertNotIn("recovery_completion_is_proven", report)

    def test_changed_inventory_after_recovery_never_gets_completion_receipt(self):
        changed = json.loads(json.dumps(self.healthy))
        changed["owners"][0]["state"]["existing_token_ids"] = [10, 11, 12]
        self.snapshot.side_effect = [self.healthy, changed]
        report = self.run_recovery()
        self.assertFalse(report["scan_complete"])
        self.assertEqual(report["guard_reason"], "token_or_auth_inventory_changed")
        self.assertFalse((self.path / "STRUCTURES-INCIDENT-RECOVERY.json").exists())

    def test_postcheck_failure_stops_and_preserves_unknown_completion_state(self):
        self.verify_after.side_effect = recovery.RecoveryGate("final_route_is_not_exclusively_restored_service")
        report = self.run_recovery()
        self.assertFalse(report["scan_complete"])
        self.assertTrue(report["supported_recovery_completed"])
        self.assertNotIn("recovery_completion_is_proven", report)
        self.Host.recover_incomplete_plan.assert_called_once()

    def test_existing_receipt_is_preserved_without_overwrite(self):
        receipt = self.path / "STRUCTURES-INCIDENT-RECOVERY.json"
        receipt.write_text("synthetic previous evidence")
        report = self.run_recovery()
        self.assertFalse(report["scan_complete"])
        self.assertEqual(report["failure_phase"], "save_completion_receipt")
        self.assertEqual(receipt.read_text(), "synthetic previous evidence")


class BackupEvidenceTests(unittest.TestCase):
    def test_real_payload_hash_and_truncated_or_mismatched_backups(self):
        with tempfile.TemporaryDirectory(prefix="buh-backup-test-") as temporary:
            root = Path(temporary)
            payload = b"synthetic-database-backup-payload"
            (root / "database.sql.gz").write_bytes(payload)
            host = SimpleNamespace(backup_path=root)
            info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600,
                                   st_size=len(payload), st_mtime_ns=1, st_dev=2, st_ino=3)
            pilot = MagicMock()
            manifest = {"filename": "database.sql.gz", "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest()}
            pilot.private_bytes.return_value = json.dumps(manifest).encode()
            with patch.object(os, "fstat", return_value=info):
                result = recovery.verify_backup(host, pilot)
                self.assertEqual(result["sha256"], manifest["sha256"])
                manifest["sha256"] = "f" * 64
                pilot.private_bytes.return_value = json.dumps(manifest).encode()
                with self.assertRaisesRegex(recovery.RecoveryGate, "retained_database_backup_hash_mismatch"):
                    recovery.verify_backup(host, pilot)
                manifest["size"] += 1
                info.st_size += 1
                pilot.private_bytes.return_value = json.dumps(manifest).encode()
                with self.assertRaisesRegex(recovery.RecoveryGate, "retained_database_backup_truncated"):
                    recovery.verify_backup(host, pilot)


class RestoredEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(app_dir=Path("/synthetic-app"), state_dir=Path("/synthetic-state"),
                                      auth_services=("web", "worker"), gunicorn_service="web", gunicorn_port=8000,
                                      nginx_upstream_file="upstream", nginx_upstream_container_file="/synthetic/upstream")
        self.host = MagicMock()
        self.host.config = self.config
        self.host.auth_replica_counts = {"web": 1, "worker": 1}
        self.host.previous_images = {"web": ("image-web", "web"), "worker": ("image-worker", "worker")}
        self.host.previous_web_slots = ("synthetic-previous",)
        self.host.candidate_web_slots = ("synthetic-candidate",)
        self.host.previous_image_pins = {"web": "synthetic-pin"}
        self.host._run.return_value = ""
        self.identities = {"web": "a" * 64, "worker": "b" * 64}
        self.host._running_service_containers.side_effect = lambda role, **kw: (self.identities[role][:12],)
        self.before = {"live_auth_services": {role: [{"container_id": identity}] for role, identity in self.identities.items()}}
        self.pilot = MagicMock()
        self.pilot.OLD_RELEASE, self.pilot.OLD_MANIFEST, self.pilot.OLD_SOURCE = "release", "manifest", "source"
        self.route = b"server web:8000;\n"
        self.pilot.private_bytes.side_effect = self.read
        self.host._proxy_exec.side_effect = lambda *args, **kw: self.route.decode()
        self.pilot.host_resources.return_value = {"cpu_count": 6}
        self.stat_patch = patch.object(os, "statvfs", return_value=SimpleNamespace(
            f_bavail=80000, f_blocks=100000, f_frsize=1024 * 1024))
        self.stat_patch.start()
        self.addCleanup(self.stat_patch.stop)

    def read(self, path, maximum):
        if str(path).endswith("/upstream"):
            return self.route
        return json.dumps({"platform_version": "0.8.2", "release_commit": "release",
                           "manifest_sha256": "manifest", "source_commit": "source"}).encode()

    def test_short_service_ids_resolve_uniquely_and_all_cleanup_and_route_evidence_is_checked(self):
        value = recovery.verify_after(self.host, self.pilot, self.before)
        self.assertEqual(value["live_service_container_ids"], {role: [identity] for role, identity in self.identities.items()})
        self.assertTrue(value["supported_safety_slots_removed"])
        self.assertTrue(value["supported_image_pins_removed"])
        self.host._require_live_images.assert_called_once()
        self.assertEqual(self.host._run.call_count, 3)
        self.host._public_smoke_checks.assert_called_once()

    def test_additional_upstream_or_remaining_safety_resources_do_not_count_as_complete(self):
        self.route += b"server synthetic-previous:8000 backup;\n"
        with self.assertRaisesRegex(recovery.RecoveryGate, "final_route_is_not_exclusively_restored_service"):
            recovery.verify_after(self.host, self.pilot, self.before)
        self.host._run.assert_not_called()
        self.route = b"server web:8000;\n"
        self.host._run.return_value = "synthetic remaining container"
        with self.assertRaisesRegex(recovery.RecoveryGate, "supported_safety_slot_cleanup_incomplete"):
            recovery.verify_after(self.host, self.pilot, self.before)



class RecoveryLauncherTests(unittest.TestCase):
    def invoke(self, *, mode="recover", stdout=None, returncode=0, wrong_hash=False, process_error=None):
        script = Path(__file__).with_name("run-retained-recovery.ps1").read_text()
        root_script = script.split("$rootScript = @'\n", 1)[1].split("\n'@\n", 1)[0]
        if stdout is None:
            stdout = json.dumps({"schema_version": 1, "scan_complete": True,
                                 "read_only": mode == "report"}).encode()
        with tempfile.TemporaryDirectory(prefix="buh-recovery-launcher-test-") as temporary:
            path = Path(temporary)
            stage = path / "private-stage"
            stage.mkdir()
            source = path / "synthetic-source.py"
            source.write_bytes(b"# synthetic source")
            digest = "f" * 64 if wrong_hash else hashlib.sha256(source.read_bytes()).hexdigest()
            argv = ["launcher", "/tmp/buh-recovery-upload.Synthetic123", digest, "a" * 40,
                    "/root/buh-structures-pilot-badc74fd-synthetic", "gh-123-1", "b" * 64,
                    base64.b64encode(b"Synthetic Pilot").decode(), mode]
            original_open = os.open

            def open_source(value, flags):
                self.assertEqual(str(value), "/tmp/buh-recovery-upload.Synthetic123/recovery.py")
                return original_open(source, flags)

            output = io.StringIO()
            with (patch("sys.argv", argv), patch("os.umask"), patch("os.open", side_effect=open_source),
                  patch("tempfile.mkdtemp", return_value=str(stage)),
                  patch("subprocess.run", side_effect=process_error, return_value=SimpleNamespace(
                      stdout=stdout, stderr=b"synthetic-secret-never-export", returncode=returncode)) as child,
                  redirect_stdout(output)):
                with self.assertRaises(SystemExit) as result:
                    exec(compile(root_script, "<retained-recovery-launcher-test>", "exec"), {"__name__": "__main__"})
            report = json.loads(output.getvalue())
            if report.get("staged_report_path"):
                self.assertEqual(json.loads(Path(report["staged_report_path"]).read_text()), report)
            return report, result.exception.code, child

    def test_complete_bounded_report_is_saved_and_passes_exact_selected_mode(self):
        report, code, child = self.invoke()
        self.assertEqual(code, 0)
        self.assertTrue(report["scan_complete"])
        self.assertEqual(child.call_args.args[0][-1], "recover")
        self.assertEqual(child.call_args.kwargs["timeout"], 1200)
        self.assertNotIn("synthetic-secret-never-export", json.dumps(report))

    def test_structured_host_failure_is_preserved_without_retry(self):
        stdout = json.dumps({"schema_version": 1, "scan_complete": False, "read_only": False,
                             "failure_phase": "verify_current_owners", "supported_recovery_attempted": False}).encode()
        report, code, child = self.invoke(stdout=stdout, returncode=1)
        self.assertEqual(code, 1)
        self.assertEqual(report["failure_phase"], "verify_current_owners")
        self.assertFalse(report["supported_recovery_attempted"])
        self.assertEqual(child.call_count, 1)

    def test_changed_source_stops_before_child_start(self):
        report, code, child = self.invoke(wrong_hash=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["mutation_result"], "not_attempted")
        self.assertEqual(report["failure_phase"], "validate_source")
        child.assert_not_called()

    def test_invalid_or_large_output_never_claims_completed_mutation(self):
        for stdout in (b"synthetic-secret-never-export", b"x" * (256 * 1024 + 1),
                       json.dumps({"schema_version": 1, "scan_complete": True, "read_only": True}).encode()):
            with self.subTest(length=len(stdout)):
                report, code, child = self.invoke(stdout=stdout)
                self.assertEqual(code, 1)
                self.assertFalse(report["scan_complete"])
                self.assertEqual(report["mutation_result"], "unknown_requires_report_review")
                self.assertNotIn("synthetic-secret-never-export", json.dumps(report))
                self.assertEqual(child.call_count, 1)

    def test_readonly_mode_failure_never_claims_a_recovery_was_started(self):
        report, code, child = self.invoke(mode="report", stdout=b"")
        self.assertEqual(code, 1)
        self.assertTrue(report["read_only"])
        self.assertEqual(report["mutation_result"], "not_attempted")
        self.assertEqual(child.call_args.args[0][-1], "report")


if __name__ == "__main__":
    unittest.main()
