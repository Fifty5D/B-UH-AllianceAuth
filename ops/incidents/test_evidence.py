"""The staged report cannot expose arbitrary logs/config or perform recovery."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ops.incidents import collect_sso_incident as host, database_report as database


class EvidenceTests(unittest.TestCase):
    def test_owner_errors_bind_exact_owner_shape_and_timestamp(self):
        owners = [{"owner_pk": 42, "corporation_name": "Synthetic Owner"},
                  {"owner_pk": 43, "corporation_name": "Other Owner"}]
        secret = "refresh_token=do-not-export"
        output = (
            "2026-10-02T03:00:00.123Z ERROR Synthetic Owner: No valid character found for sync. "
            "Service down for this owner. " + secret + "\n"
            "2026-10-02T03:00:01Z ERROR Different Synthetic Owner: No valid character found for sync. "
            "Service down for this owner.\n"
            "2026-10-02T03:00:02Z ERROR Other Owner: unrelated secret " + secret + "\n"
        )
        result = host.log_owner_evidence(output, owners)
        self.assertEqual(set(result), {"42"})
        self.assertEqual(result["42"]["first"], "2026-10-02T03:00:00.123Z")
        self.assertNotIn(secret, json.dumps(result))

    def test_marked_database_output_never_returns_unrelated_startup_logs(self):
        payload = {"read_only": True, "scan_complete": True, "summary": {}}
        output = ("startup secret=do-not-export\nBUH_INCIDENT_REPORT_BEGIN\n"
                  + json.dumps(payload) + "\nBUH_INCIDENT_REPORT_END\nother secret\n")
        self.assertEqual(host.parse_database_output(output), payload)
        for invalid in (output + output, output.replace("BUH_INCIDENT_REPORT_END", "missing"),
                        output.replace('"read_only": true', '"read_only": false')):
            with self.assertRaises(ValueError):
                host.parse_database_output(invalid)

    def test_container_credentials_commands_and_unrelated_labels_are_removed(self):
        secret = "do-not-export"
        value = {
            "Id": "a" * 64, "Name": "/synthetic-worker", "Image": "sha256:" + "b" * 64,
            "Config": {"Image": "synthetic@sha256:" + "b" * 64, "Env": [secret],
                       "Cmd": [secret], "Labels": {"secret": secret,
                                                  "com.b-uh.platform.version": "0.8.2"}},
            "State": {"Status": "running", "Running": True, "StartedAt": "now",
                      "Health": {"Log": [secret]}},
            "RestartCount": 0, "NetworkSettings": {"Networks": {"auth": {}}},
            "Mounts": [],
        }
        result = host.container_projection(value)
        self.assertEqual(result["labels"], {"com.b-uh.platform.version": "0.8.2"})
        self.assertNotIn(secret, json.dumps(result))

    def test_arbitrary_error_text_is_hashed_not_exported_or_declared_permanent(self):
        secret = "raw-refresh-token-do-not-export"
        result = database.error_evidence("TokenDoesNotExist invalid_grant " + secret)
        self.assertEqual(result["classes"], ["TokenDoesNotExist"])
        self.assertEqual(result["oauth_codes"], ["invalid_grant"])
        self.assertTrue(result["requires_fresh_validation"])
        self.assertNotIn(secret, json.dumps(result))

    def test_write_boundary_rejects_mutation_and_locking_reads(self):
        execute = mock.Mock(return_value="selected")
        for sql in ("UPDATE table SET value=1", "DELETE FROM table", "INSERT INTO table VALUES (1)",
                    "SELECT * FROM table FOR UPDATE", "SELECT 1; DELETE FROM table",
                    "SELECT 1 INTO OUTFILE '/tmp/out'"):
            with self.assertRaises(RuntimeError):
                database.reject_writes(execute, sql, [], False, {})
        execute.assert_not_called()
        self.assertEqual(database.reject_writes(execute, "SELECT 1", [], False, {}), "selected")

    def test_report_bounds_fail_closed_without_partial_success(self):
        with self.assertRaises(database.EvidenceBoundExceeded):
            database.bounded(list(range(4)), 3)
        self.assertEqual(database.bounded(list(range(3)), 3), [0, 1, 2])

    def test_exception_messages_never_cross_report_boundary(self):
        with (mock.patch.object(host, "collect", side_effect=ValueError("password=do-not-export")),
              mock.patch("sys.stdout", new_callable=io.StringIO) as output):
            self.assertEqual(host.main(), 1)
        self.assertEqual(json.loads(output.getvalue())["error_type"], "ValueError")
        self.assertNotIn("do-not-export", output.getvalue())

    @unittest.skipUnless(os.name == "posix", "Linux evidence safety boundary")
    def test_evidence_rejects_writable_ancestor_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o777)
            path = root / "evidence.json"
            path.write_text("secret", encoding="ascii")
            with mock.patch.object(os, "open") as opened:
                with self.assertRaises(ValueError):
                    host.read_file(path)
            opened.assert_not_called()

    def test_staged_launcher_python_is_parseable_and_archives_only_diagnostics(self):
        launcher = Path(__file__).with_name("run-sso-incident.ps1").read_text()
        embedded = launcher.split("$rootScript = @'\n", 1)[1].split("\n'@", 1)[0]
        compile(embedded, "staged-launcher", "exec")
        self.assertIn('wanted = {"collect_sso_incident.py": collector_hash, "database_report.py": database_hash}', launcher)
        self.assertNotIn("recover_incomplete_plan", launcher)

    def test_private_plan_projection_never_exports_unrecognized_content(self):
        secret = "do-not-export"
        value = {"attempt_id": host.ATTEMPT, "flags": {"migration_started": True},
                 "local_settings": secret, "token": secret}
        projected = host.projection(value, host.PLAN_FIELDS)
        self.assertNotIn(secret, json.dumps(projected))
        self.assertEqual(projected["attempt_id"], host.ATTEMPT)


if __name__ == "__main__":
    unittest.main()
