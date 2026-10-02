"""The staged report cannot expose arbitrary logs/config or perform recovery."""
import io
import json
import os
from pathlib import Path
import tempfile
import stat
from types import SimpleNamespace
import unittest
import urllib.error
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

    def test_multiline_metadata_reads_are_permitted_and_writes_still_blocked(self):
        execute = mock.Mock(return_value="selected")
        for sql in ("  SELECT\n table_name FROM information_schema.tables",
                    "SHOW\tTABLES", "EXPLAIN\nSELECT 1"):
            self.assertEqual(database.reject_writes(
                execute, sql, [], False, {}), "selected")
        execute.reset_mock()
        for sql in ("SELECTOR table", "SELECT\n1;\nDELETE FROM table",
                    "SELECT * FROM table FOR\nUPDATE",
                    "SELECT 1 INTO\nOUTFILE '/tmp/out'",
                    "SELECT 1 INTO\tDUMPFILE '/tmp/out'"):
            with self.assertRaises(RuntimeError):
                database.reject_writes(execute, sql, [], False, {})
        execute.assert_not_called()

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

    def test_public_identity_reads_do_not_use_credentials_and_project_only_identity(self):
        database = {"structures": [{
            "owner_pk": 42, "corporation_id": 10001, "enabled": True,
            "configured_characters": [], "other_linked_characters_in_stored_corporation": [],
        }], "memberaudit": []}
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers = {}
        response.read.return_value = json.dumps({"ceo_id": 90001, "name": "Synthetic",
                                                "unrecognized": "do-not-export"}).encode()
        opener = mock.Mock(return_value=response)
        result = host.public_identity_inventory(database, set(), opener=opener)
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["records"][0]["identity"]["ceo_id"], 90001)
        request = opener.call_args.args[0]
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("do-not-export", json.dumps(result))

    def test_provider_throttling_stops_further_public_requests(self):
        database = {"structures": [{
            "owner_pk": 42, "corporation_id": 10001, "enabled": True,
            "configured_characters": [
                {"enabled": False, "identity": {"character_id": 90001}},
            ], "other_linked_characters_in_stored_corporation": [],
        }], "memberaudit": []}
        opener = mock.Mock(side_effect=urllib.error.HTTPError(
            "https://esi.evetech.net/", 429, "do-not-export", {}, None))
        result = host.public_identity_inventory(database, {"42"}, opener=opener)
        opener.assert_called_once()
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["records"][1]["result"], "skipped_provider_throttling")
        self.assertNotIn("do-not-export", json.dumps(result))

    def test_private_plan_projection_never_exports_unrecognized_content(self):
        secret = "do-not-export"
        value = {"attempt_id": host.ATTEMPT, "flags": {"migration_started": True},
                 "local_settings": secret, "token": secret}
        projected = host.projection(value, host.PLAN_FIELDS)
        self.assertNotIn(secret, json.dumps(projected))
        self.assertEqual(projected["attempt_id"], host.ATTEMPT)


    def test_expected_retained_static_directory_is_metadata_only(self):
        details = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0, st_gid=0,
                                  st_size=4096, st_mtime=1700000000)
        with (mock.patch.object(Path, "lstat", return_value=details),
              mock.patch.object(host.os, "open", return_value=41),
              mock.patch.object(host.os, "fstat", return_value=details),
              mock.patch.object(host.os, "close") as closed,
              mock.patch.object(host.os, "fdopen") as reader):
            metadata, data = host.read_file(
                "/root/retained/previous-static-root", allow_directory=True)
        self.assertEqual(metadata["kind"], "directory")
        self.assertEqual(metadata["content_audit"], "not_recursively_verified")
        self.assertIsNone(data)
        reader.assert_not_called()
        closed.assert_called_once_with(41)

    def test_unknown_directory_is_still_rejected_as_a_payload(self):
        details = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0, st_gid=0)
        with (mock.patch.object(Path, "lstat", return_value=details),
              mock.patch.object(host.os, "open", return_value=41),
              mock.patch.object(host.os, "fstat", return_value=details),
              mock.patch.object(host.os, "close")):
            with self.assertRaises(ValueError):
                host.read_file("/root/retained/unexpected-directory")

    def test_backup_inventory_continues_after_one_unsafe_entry(self):
        directory = mock.Mock(spec=Path)
        directory.lstat.return_value = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700, st_uid=0)
        paths = [Path("/root/retained/" + name) for name in
                 ("BACKUP.json", "previous-static-root", "staticfiles.previous.json",
                  "unsafe.json")]
        directory.iterdir.return_value = paths

        def reader(path, **kwargs):
            if path.name == "unsafe.json":
                raise ValueError("secret=do-not-export")
            if path.name == "previous-static-root":
                self.assertTrue(kwargs["allow_directory"])
                return {"kind": "directory", "hash_status": "not_applicable_directory"}, None
            self.assertFalse(kwargs["allow_directory"])
            return {"kind": "file", "sha256": "a" * 64}, b"synthetic"

        with mock.patch.object(host, "read_file", side_effect=reader):
            entries, errors = host.backup_inventory(
                directory, {"staticfiles.previous.json": "a" * 64})
        self.assertEqual(len(entries), 4)
        self.assertEqual(errors, [{"section": "backup_inventory",
                                  "entry": "unsafe.json", "error_type": "ValueError"}])
        saved = next(row for row in entries if row["name"] == "staticfiles.previous.json")
        self.assertTrue(saved["matches_expected_sha256"])
        self.assertNotIn("do-not-export", json.dumps((entries, errors)))

    def test_database_failure_sites_do_not_export_messages_or_locals(self):
        namespace = {"__name__": "synthetic"}
        program = host.database_program(
            "def broken():\n    raise AttributeError('secret=never-export')\n"
            "def emit():\n    broken()\n")
        try:
            exec(program, namespace)
        except AttributeError as exc:
            evidence = database.failure_evidence(exc)
        self.assertEqual(evidence["error_type"], "AttributeError")
        self.assertIn({"function": "broken", "line": 2}, evidence["report_frames"])
        self.assertNotIn("never-export", json.dumps(evidence))


if __name__ == "__main__":
    unittest.main()
