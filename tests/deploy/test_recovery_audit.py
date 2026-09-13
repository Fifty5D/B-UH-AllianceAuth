"""Independent failures remain visible; diagnosis cannot install or authorize."""

from types import SimpleNamespace
from pathlib import Path
import os
import shutil
import subprocess
import unittest
from unittest import mock

from ops.deploy import recovery_audit as audit
from ops.deploy.contracts import DeploymentError
from ops.deploy.docker_host import LogScanError


class RecoveryAuditTests(unittest.TestCase):
    def test_owner_launcher_has_valid_embedded_python_and_powershell(self):
        launcher = Path(__file__).resolve().parents[2] / "ops/deploy/run-recovery-audit.ps1"
        text = launcher.read_text()
        embedded = text.split("$rootScript = @'\n", 1)[1].split("\n'@", 1)[0]
        compile(embedded, str(launcher), "exec")
        powershell = shutil.which("pwsh")
        if powershell is None:
            if os.environ.get("GITHUB_ACTIONS") == "true":
                self.fail("Hosted Linux must validate the owner PowerShell launcher")
            return
        result = subprocess.run([
            powershell, "-NoProfile", "-NonInteractive", "-Command",
            "$tokens=$null; $errors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile($env:BUH_AUDIT_SCRIPT,[ref]$tokens,[ref]$errors) | Out-Null; "
            "if ($errors.Count) { $errors | Out-String | Write-Error; exit 1 }",
        ], env={**os.environ, "BUH_AUDIT_SCRIPT": str(launcher)}, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_all_independent_checks_run_and_findings_stay_separate(self):
        calls = []

        def fail():
            calls.append("failed")
            raise DeploymentError("Django failed password=secret-value")

        def logs():
            raise LogScanError(["worker: HTTPError()", "proxy: CRITICAL broken"])

        results = audit.run_checks([
            ("django", fail), ("public-http", lambda: calls.append("http")),
            ("retained-interval-logs", logs),
        ])
        self.assertEqual(calls, ["failed", "http"])
        self.assertEqual([r["result"] for r in results], ["failed", "passed", "failed"])
        self.assertNotIn("secret-value", str(results))
        self.assertEqual(len(results[-1]["findings"]), 2)

    def test_failed_context_blocks_owner_exceptions_but_not_functional_diagnosis(self):
        host = mock.Mock(recovery_baseline_verified=True)
        host.restored_health_checks.return_value = [("redis", lambda: None)]
        with mock.patch.object(audit.recovery, "prepare_verification", side_effect=DeploymentError("context changed")):
            result = audit.diagnose(host, {}, SimpleNamespace())
        self.assertFalse(host.recovery_baseline_verified)
        self.assertEqual(result["checks"][-1]["result"], "passed")
        self.assertFalse(result["all_checks_passed"])
        host.rollback.assert_not_called()
        host._retain_transition_log_findings.assert_not_called()

    def test_successful_diagnosis_never_creates_recovery_authority(self):
        host = mock.Mock()
        host.restored_health_checks.return_value = [("public-http", lambda: None)]
        with (
            mock.patch.object(audit.recovery, "prepare_verification"),
            mock.patch.object(audit.recovery, "restored_file_checks", return_value=[]),
        ):
            result = audit.diagnose(host, {}, SimpleNamespace())
        self.assertTrue(result["all_checks_passed"])
        for key in ("authorizes_cleanup", "authorizes_deployment", "verification_receipt_created"):
            self.assertFalse(result[key])
        self.assertTrue(result["preserve_resources"])
        host.rollback.assert_not_called()

    def test_changed_installed_hash_stops_before_probes_or_receiver_install(self):
        with (
            mock.patch.object(audit.os, "geteuid", return_value=0, create=True),
            mock.patch.object(audit.recovery, "verify_pins", side_effect=DeploymentError("hash changed")) as pins,
            mock.patch.object(audit, "_open_lock") as lock,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(audit.main(["--runtime-sha256", "a" * 64, "--archive", "unused"]), 1)
        pins.assert_called_once_with("a" * 64, installed_sha256=audit.INSTALLED_RUNTIME)
        lock.assert_not_called()
