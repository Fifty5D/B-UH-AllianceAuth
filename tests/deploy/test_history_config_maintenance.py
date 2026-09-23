from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ops.deploy import history_config_maintenance as maintenance
from ops.deploy.contracts import DeploymentError, canonical_json_bytes


ROOT = Path(__file__).resolve().parents[2]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class HistoryConfigMaintenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        etc = root / "etc"
        state = root / "state"
        receiver_backup = root / "receiver-backup"
        maintenance_backup = root / "maintenance-backup"
        runtime = root / "library" / "docker_host.py"
        for directory in (
            etc,
            state,
            receiver_backup,
            maintenance_backup,
            runtime.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)

        source = json.loads((ROOT / "ops/deploy/receiver-config.example.json").read_text())
        source["state_dir"] = str(state)
        source["backup_dir"] = str(receiver_backup)
        del source["setup_arguments"]["buh_archive_setup"]
        self.original = canonical_json_bytes(source)
        config = etc / "receiver.json"
        self._write(config, self.original)

        install = etc / "INSTALL.json"
        self.install_value = {
            "schema_version": 1,
            "source_commit": "a" * 40,
            "config_sha256": _sha256(self.original),
            "files": {},
        }
        self._write(install, canonical_json_bytes(self.install_value))
        parent = etc / "OWNER-EXHAUSTION-REPAIR.json"
        self._write(parent, b'{"repair":"reviewed"}\n')
        runtime_bytes = b"# installed reviewed receiver runtime\n"
        self._write(runtime, runtime_bytes)
        completion = state / maintenance.COMPLETION_NAME
        self.completion_value = {
            "schema_version": 1,
            "attempt_id": maintenance.recovery.ATTEMPT,
            "result": "restored-production-verified-with-reviewed-history",
            "cleanup": "passed",
            "deployment_performed": False,
            "database_restored": False,
            "migrations_reversed": False,
            "retained_plan_sha256": maintenance.recovery.PLAN_SHA256,
            "original_journal_sha256": maintenance.recovery.JOURNAL_SHA256,
            "runtime_sha256": _sha256(runtime_bytes),
            "installed_runtime_sha256": _sha256(runtime_bytes),
            "archive_sha256": "b" * 64,
            "review_sha256": "c" * 64,
            "historical_interval_clean": False,
            "reviewed_log_until": "2026-09-14T18:06:30+00:00",
            "retained_interval_continuity_verified": True,
            "worker_log_start_coverage": [
                {
                    "container": f"{index:012x}",
                    "boundary_record_at": "2026-09-14T18:06:00+00:00",
                    "boundary_record_sha256": "a" * 64,
                }
                for index in range(1, 7)
            ],
            "checks": [
                {"check": name, "result": "passed"} for name in maintenance.RECOVERY_CHECKS
            ],
            "recovery": "Verified prior images and traffic restored; backup retained.",
        }
        self._write(completion, canonical_json_bytes(self.completion_value))
        self.paths = maintenance.MaintenancePaths(
            config=config,
            install=install,
            parent_receipt=parent,
            receipt=etc / "HISTORY-CONFIG-MAINTENANCE.json",
            runtime=runtime,
            backup_root=maintenance_backup,
        )
        # Model root-owned fixture files without requiring the test runner to
        # be root. Reads, mode checks, atomic replacement and fsync remain real.
        real_fstat = os.fstat
        real_lstat = Path.lstat

        def root_owned(details):
            values = list(details)
            values[4:6] = [0, 0]
            return os.stat_result(values)

        self.patches = (
            mock.patch.object(
                maintenance, "_verify_root_owned_ancestors", return_value=None
            ),
            mock.patch.object(
                maintenance.recovery,
                "verify_exhaustion_repair",
                return_value=None,
            ),
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(
                os, "fstat", side_effect=lambda fd: root_owned(real_fstat(fd))
            ),
            mock.patch.object(
                Path, "lstat", autospec=True,
                side_effect=lambda path: root_owned(real_lstat(path)),
            ),
            mock.patch.object(
                os, "fchown", create=True,
                side_effect=AssertionError("Fixtures must not require privileged chown"),
            ),
            mock.patch.object(maintenance, "_open_lock", side_effect=self._lock),
        )
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.write_bytes(data)
        path.chmod(0o600)

    def _lock(self, config) -> int:
        descriptor = os.open(
            config.state_dir / "deploy.lock", os.O_RDWR | os.O_CREAT, 0o600
        )
        os.chmod(config.state_dir / "deploy.lock", 0o600)
        return descriptor

    def _plan_and_apply(self) -> tuple[dict, dict]:
        plan = maintenance.build_plan(self.paths, verify_repairs=False)
        result = maintenance.apply(
            old_config_sha256=plan["old_config_sha256"],
            new_config_sha256=plan["new_config_sha256"],
            provenance_sha256=plan["provenance_sha256"],
            paths=self.paths,
            verify_repairs=False,
        )
        return plan, result

    def test_apply_preserves_config_and_provenance_and_repeat_is_idempotent(self):
        before = json.loads(self.original)
        install_before = self.paths.install.read_bytes()
        parent_before = self.paths.parent_receipt.read_bytes()
        completion = (
            Path(json.loads(self.original)["state_dir"]) / maintenance.COMPLETION_NAME
        )
        completion_before = completion.read_bytes()

        plan, result = self._plan_and_apply()
        after = json.loads(self.paths.config.read_bytes())
        added = after["setup_arguments"].pop("buh_archive_setup")
        self.assertEqual(added, ["--no-color"])
        self.assertEqual(after, before)
        self.assertEqual(self.paths.install.read_bytes(), install_before)
        self.assertEqual(self.paths.parent_receipt.read_bytes(), parent_before)
        self.assertEqual(completion.read_bytes(), completion_before)
        self.assertEqual(result["previous_sha256"], plan["old_config_sha256"])
        self.assertFalse(result["deployment_performed"])
        self.assertFalse(result["services_restarted"])

        repeated = maintenance.apply(
            old_config_sha256=plan["old_config_sha256"],
            new_config_sha256=plan["new_config_sha256"],
            provenance_sha256=plan["provenance_sha256"],
            paths=self.paths,
            verify_repairs=False,
        )
        self.assertEqual(repeated["result"], "already-applied")

    def test_pre_recovery_state_is_blocked(self):
        state = Path(json.loads(self.original)["state_dir"])
        self._write(state / "active-recovery.json", b"{}\n")
        with self.assertRaisesRegex(DeploymentError, "Recovery is still active"):
            maintenance.build_plan(self.paths, verify_repairs=False)

    def test_untrusted_file_owner_is_blocked(self):
        fixture_stat = os.fstat

        def untrusted_owner(descriptor):
            values = list(fixture_stat(descriptor))
            values[4] = 1001
            return os.stat_result(values)

        with mock.patch.object(os, "fstat", side_effect=untrusted_owner):
            with self.assertRaisesRegex(DeploymentError, "provenance is unsafe"):
                maintenance.build_plan(self.paths, verify_repairs=False)

    def test_incomplete_recovery_receipt_is_blocked(self):
        state = Path(json.loads(self.original)["state_dir"])
        completion = state / maintenance.COMPLETION_NAME
        value = json.loads(completion.read_bytes())
        value["checks"] = value["checks"][:-1]
        self._write(completion, canonical_json_bytes(value))
        with self.assertRaisesRegex(DeploymentError, "completion receipt is incomplete"):
            maintenance.build_plan(self.paths, verify_repairs=False)

    def test_documented_log_gap_receipt_preserves_provenance_for_history_setup(self):
        state = Path(json.loads(self.original)["state_dir"])
        completion = state / maintenance.COMPLETION_NAME
        value = json.loads(completion.read_bytes())
        fresh = "2026-09-23T12:48:00+00:00"
        value.update(
            result="restored-production-verified-with-reviewed-log-gap",
            reviewed_log_until="2026-09-14T18:06:30+00:00",
            retained_interval_continuity_verified=False,
            retained_log_gap={
                "policy": "documented-worker-log-rotation-v1",
                "unverified_since": "2026-09-14T18:06:30+00:00",
                "fresh_scan_since": fresh,
                "diagnostic_report_sha256": "d" * 64,
                "retained_grouping_sha256": "e" * 64,
            },
            worker_log_start_coverage=[
                {
                    "container": f"{index:012x}",
                    "boundary_record_at": "2026-09-23T12:47:00+00:00",
                    "boundary_record_sha256": "a" * 64,
                }
                for index in range(1, 7)
            ],
        )
        self._write(completion, canonical_json_bytes(value))
        maintenance.build_plan(self.paths, verify_repairs=False)
        value["retained_interval_continuity_verified"] = True
        self._write(completion, canonical_json_bytes(value))
        with self.assertRaisesRegex(DeploymentError, "gap receipt is incomplete"):
            maintenance.build_plan(self.paths, verify_repairs=False)

    def test_apply_requires_present_and_matching_plan_pins(self):
        plan = maintenance.build_plan(self.paths, verify_repairs=False)
        for old, new, provenance in (
            (None, plan["new_config_sha256"], plan["provenance_sha256"]),
            ("f" * 64, plan["new_config_sha256"], plan["provenance_sha256"]),
            (plan["old_config_sha256"], "f" * 64, plan["provenance_sha256"]),
            (plan["old_config_sha256"], plan["new_config_sha256"], "f" * 64),
        ):
            with self.subTest(old=old, new=new, provenance=provenance):
                with self.assertRaises(DeploymentError):
                    maintenance.apply(
                        old_config_sha256=old,
                        new_config_sha256=new,
                        provenance_sha256=provenance,
                        paths=self.paths,
                        verify_repairs=False,
                    )
        self.assertEqual(self.paths.config.read_bytes(), self.original)

    def test_applied_receipt_rejects_changed_live_provenance(self):
        self._plan_and_apply()
        self._write(self.paths.parent_receipt, b'{"repair":"changed"}\n')
        with self.assertRaisesRegex(DeploymentError, "receipt does not match"):
            maintenance.build_plan(self.paths, verify_repairs=False)

    def test_receipt_write_failure_rolls_back_and_retry_reuses_backup(self):
        plan = maintenance.build_plan(self.paths, verify_repairs=False)
        atomic = maintenance._atomic_bytes
        failed_once = False

        def fail_receipt_once(path, data, mode, *, owner=None):
            nonlocal failed_once
            if path == self.paths.receipt and not failed_once:
                failed_once = True
                raise OSError("synthetic receipt write failure")
            return atomic(path, data, mode, owner=owner)

        with mock.patch.object(maintenance, "_atomic_bytes", side_effect=fail_receipt_once):
            with self.assertRaisesRegex(DeploymentError, "prior config restored"):
                maintenance.apply(
                    old_config_sha256=plan["old_config_sha256"],
                    new_config_sha256=plan["new_config_sha256"],
                    provenance_sha256=plan["provenance_sha256"],
                    paths=self.paths,
                    verify_repairs=False,
                )
        self.assertEqual(self.paths.config.read_bytes(), self.original)
        self.assertFalse(self.paths.receipt.exists())

        result = maintenance.apply(
            old_config_sha256=plan["old_config_sha256"],
            new_config_sha256=plan["new_config_sha256"],
            provenance_sha256=plan["provenance_sha256"],
            paths=self.paths,
            verify_repairs=False,
        )
        self.assertEqual(result["result"], "history-setup-command-configured")


if __name__ == "__main__":
    unittest.main()
