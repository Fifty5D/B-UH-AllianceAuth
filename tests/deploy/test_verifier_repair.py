
"""Single-file activation is gated, backed up and restores its exact input on failure."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from ops.deploy import verifier_repair as repair
from ops.deploy.contracts import DeploymentError
from ops.deploy.docker_host import DockerHost
from tests.deploy.test_verifier_corrections import retained_host


class VerifierActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / "library"
        target = self.library / "ops/deploy/docker_host.py"
        target.parent.mkdir(parents=True)
        self.old, self.new = b"VALUE = 1\n", b"VALUE = 2\n"
        target.write_bytes(self.old)
        self.target = target
        self.candidate = self.root / "candidate.py"
        self.candidate.write_bytes(self.new)
        self.review = self.root / "review.json"
        self.review.write_bytes(b'{"attempt_id":"gh-123-1"}')
        self.backups = self.root / "receiver-backups"
        self.backups.mkdir()
        self.host_backup = self.root / "retained-attempt"
        self.host_backup.mkdir()
        self.host = SimpleNamespace(backup_path=self.host_backup,
                                    retained_verifier_review={"attempt_id": "gh-123-1"})
        self.receipt = self.root / "REPAIR.json"
        self.original_bytes = b'{"source_commit":"synthetic-original"}'
        real_private = repair.private_read
        def read(path, *args, **kwargs):
            if str(path) == "/etc/buh-platform-v2/INSTALL.json":
                return self.original_bytes
            return Path(path).read_bytes()
        self.enterContext(mock.patch.object(repair, "private_read", side_effect=read))
        self.enterContext(mock.patch.object(repair, "LIBRARY", self.library))
        self.enterContext(mock.patch.object(repair, "RECEIPT", self.receipt))
        self.enterContext(mock.patch.object(repair, "BASE_DOCKER_SHA256", repair.digest(self.old)))
        self.enterContext(mock.patch.object(repair, "_verify_root_owned_ancestors"))
        self.enterContext(mock.patch.object(repair, "installed_identity", return_value={}))
        self.real_private = real_private
        real_path = repair.Path
        def paths(value):
            return self.backups if str(value) == "/var/backups/buh-receiver-upgrade" else real_path(value)
        self.enterContext(mock.patch.object(repair, "Path", side_effect=paths))
        def atomic(path, raw, mode, **kwargs):
            Path(path).write_bytes(raw)
            Path(path).chmod(mode)
        self.enterContext(mock.patch.object(repair, "_atomic_bytes", side_effect=atomic))

    def activate(self):
        return repair.install_single_file(self.host, self.candidate, repair.digest(self.new),
                                          self.review, "a" * 40)

    def test_only_runtime_override_changes_and_original_install_and_evidence_are_retained(self):
        held = self.host_backup / "RECOVERY.json"
        held.write_bytes(b"original retained plan")
        result = self.activate()
        self.assertEqual(self.target.read_bytes(), self.new)
        self.assertEqual(held.read_bytes(), b"original retained plan")
        backup = Path(result["backup_path"])
        self.assertEqual((backup / "docker_host.py").read_bytes(), self.old)
        self.assertEqual((backup / "INSTALL.json").read_bytes(), self.original_bytes)
        self.assertEqual((backup / "VERIFIER-REVIEW.json").read_bytes(), self.review.read_bytes())
        self.assertFalse(result["deployment_performed"])
        self.assertFalse(result["memberaudit_mutation_performed"])
        self.assertEqual(json.loads(self.receipt.read_bytes())["sha256"], repair.digest(self.new))

    def test_failed_installed_validation_restores_the_exact_old_runtime_and_keeps_backups(self):
        with mock.patch.object(repair, "installed_identity", side_effect=DeploymentError("synthetic mismatch")):
            with self.assertRaises(DeploymentError):
                self.activate()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertFalse(self.receipt.exists())
        self.assertEqual(next(self.backups.glob("*/docker_host.py")).read_bytes(), self.old)

    def test_source_mismatch_or_existing_receipt_stops_before_replacement(self):
        self.candidate.write_bytes(b"unexpected")
        with self.assertRaises(DeploymentError):
            self.activate()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.candidate.write_bytes(self.new)
        self.receipt.write_bytes(b"existing evidence")
        with self.assertRaises(DeploymentError):
            self.activate()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(self.receipt.read_bytes(), b"existing evidence")

    def test_all_independent_checks_are_read_and_any_failure_remains_visible(self):
        success = mock.Mock(return_value=None)
        fail = mock.Mock(side_effect=DeploymentError("synthetic fatal"))
        warning = mock.Mock(return_value=("recovered warning",))
        host = SimpleNamespace(restored_health_checks=lambda *_: [
            ("services-and-restarts", fail), ("django", success), ("retained-interval-logs", warning)])
        checks, warnings = repair.all_checks(host)
        self.assertEqual([item["passed"] for item in checks], [False, True, True])
        self.assertEqual(warnings, ["recovered warning"])
        success.assert_called_once()

    def test_absolute_and_parent_traversal_inputs_fail_before_read(self):
        for path in ("relative.json", "/root/../tmp/assessment.json"):
            with self.subTest(path=path), self.assertRaises(DeploymentError):
                self.real_private(path)


class CompletedMemberAuditReferenceTests(unittest.TestCase):
    def test_incomplete_or_wrong_completed_population_cannot_be_recovered_or_cleared(self):
        for completed in ({}, {"recovery_complete": False},
                          {"recovery_complete": True, "recovery_source_commit": "other"}):
            with self.subTest(completed=completed), self.assertRaises(DeploymentError):
                repair.memberaudit_refs(completed, [])

    def test_no_auth_or_token_update_operations_exist_in_read_only_preservation_program(self):
        self.assertIn("SET TRANSACTION READ ONLY", repair.MA_READ_CODE)
        for forbidden in (".save(", ".delete(", ".update(", ".refresh(", "bulk_refresh(", "has_token_error ="):
            self.assertNotIn(forbidden, repair.MA_READ_CODE)


class SupportedCleanupOrderingTests(unittest.TestCase):
    def test_fresh_memberaudit_or_structures_failure_preserves_all_safety_resources(self):
        for failure in ("memberaudit", "structures"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                host, _ = retained_host(Path(temp))
                calls = []
                def member(current):
                    calls.append("memberaudit")
                    if failure == "memberaudit":
                        raise DeploymentError("synthetic changed recovered section")
                def structures(current):
                    calls.append("structures")
                    if failure == "structures":
                        raise DeploymentError("synthetic unhealthy Structures owner")
                host.__class__ = repair.reconciliation_host(DockerHost, member, structures)
                with mock.patch.object(host, "_restore_candidate_configuration"), mock.patch.object(
                        DockerHost, "_verify_restored", side_effect=lambda *_: calls.append("health")), mock.patch.object(
                        host, "_remove_web_slots") as cleanup, mock.patch.object(
                        host, "_discard_previous_image_pins") as pins, mock.patch.object(
                        host, "complete_recovery_plan") as complete, self.assertRaises(DeploymentError):
                    host.rollback(None, "candidate-slot-start-1")
                cleanup.assert_not_called()
                pins.assert_not_called()
                complete.assert_not_called()
                self.assertEqual(calls[0:2], ["health", "memberaudit"])
                self.assertTrue((host.config.state_dir / "active-recovery.json").exists())

    def test_all_health_and_preservation_gates_precede_supported_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            host, _ = retained_host(Path(temp))
            order = []
            host.__class__ = repair.reconciliation_host(
                DockerHost, lambda *_: order.append("memberaudit"), lambda *_: order.append("structures"))
            with mock.patch.object(host, "_restore_candidate_configuration"), mock.patch.object(
                    DockerHost, "_verify_restored", side_effect=lambda *_: order.append("health")), mock.patch.object(
                    host, "_remove_web_slots", side_effect=lambda *_: order.append("slots")), mock.patch.object(
                    host, "_discard_previous_image_pins", side_effect=lambda *_: order.append("pins")), mock.patch.object(
                    host, "complete_recovery_plan", side_effect=lambda *_: order.append("hold")):
                host.rollback(None, "candidate-slot-start-1")
            self.assertEqual(order, ["health", "memberaudit", "structures", "slots", "pins", "hold"])
