"""Exercise the actual root launcher extracted from PowerShell, with no SSH/production."""
from contextlib import redirect_stdout
import base64
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import zipfile


SOURCE = Path(__file__).with_name("run-memberaudit-recovery.ps1").read_text(encoding="utf-8")
ROOT_SCRIPT = SOURCE.split("$rootScript = @'\n", 1)[1].split("\n'@\n", 1)[0]


class MemberAuditLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="buh-launcher-test-")
        self.addCleanup(self.temporary.cleanup)
        self.upload = Path(self.temporary.name)
        self.stage = self.upload / "private-stage"
        self.stage.mkdir()
        self.sources = {
            "run_memberaudit_recovery.py": b"# synthetic host fixture\n",
            "memberaudit_recovery.py": b"# synthetic recovery fixture\n",
            "memberaudit_selection.py": b"# synthetic selector fixture\n",
        }
        self.archive = self.upload / "pilot.zip"
        with zipfile.ZipFile(self.archive, "w") as zipped:
            for name, content in self.sources.items():
                zipped.writestr(name, content)
        self.digests = {name: hashlib.sha256(content).hexdigest() for name, content in self.sources.items()}

    def payload(self, mode="report", size=0):
        return json.dumps({
            "schema_version": 1, "scan_complete": True, "read_only": mode == "report",
            "scope": "memberaudit_october_outage", "retained_incident_evidence": {"synthetic_padding": "x" * size},
        }).encode()

    def invoke(self, *, mode="report", stdout=None, stderr=b"", returncode=0, corrupt_source=False):
        if stdout is None:
            stdout = self.payload(mode)
        argv = ["launcher", "/tmp/buh-incident-upload.Synthetic123", hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                "a" * 40, "f" * 64 if corrupt_source else self.digests["run_memberaudit_recovery.py"],
                self.digests["memberaudit_recovery.py"], self.digests["memberaudit_selection.py"], "gh-111111-1",
                base64.b64encode(b"Synthetic Pilot").decode(), mode, "/root/buh-structures-pilot-aaaaaaaa-synthetic123"]
        output = io.StringIO()
        original_open = os.open

        def open_archive(path, flags):
            self.assertEqual(str(path), "/tmp/buh-incident-upload.Synthetic123/pilot.zip")
            return original_open(self.archive, flags)

        with (patch("sys.argv", argv), patch("os.umask"), patch("os.open", side_effect=open_archive),
              patch("tempfile.mkdtemp", return_value=str(self.stage)),
              patch("subprocess.run", return_value=SimpleNamespace(
                  stdout=stdout, stderr=stderr, returncode=returncode)) as process,
              redirect_stdout(output)):
            with self.assertRaises(SystemExit) as exited:
                exec(compile(ROOT_SCRIPT, "<buh-root-launcher-test>", "exec"), {"__name__": "__main__"})
        return json.loads(output.getvalue()), exited.exception.code, process

    def test_large_retained_readonly_report_crosses_old_limit_and_is_saved_complete(self):
        stdout = self.payload(size=700 * 1024)
        report, code, process = self.invoke(stdout=stdout)
        self.assertEqual(code, 0)
        self.assertTrue(report["scan_complete"])
        self.assertEqual(report["launcher_output"]["stdout_bytes"], len(stdout))
        self.assertGreater(report["launcher_output"]["stdout_bytes"], 256 * 1024)
        self.assertEqual(report["launcher_output"]["limit_bytes"], 2 * 1024 * 1024)
        saved = json.loads((self.stage / "memberaudit-report.json").read_text())
        self.assertEqual(saved, report)
        self.assertEqual(len(saved["retained_incident_evidence"]["synthetic_padding"]), 700 * 1024)
        self.assertEqual(process.call_args.args[0][-2], "report")
        self.assertEqual(process.call_args.kwargs["timeout"], 3300)

    def test_readonly_oversize_still_stops_with_safe_size_and_phase_evidence(self):
        stdout = self.payload(size=10 * 1024 * 1024)
        report, code, _ = self.invoke(stdout=stdout)
        self.assertEqual(code, 1)
        self.assertFalse(report["scan_complete"])
        self.assertEqual(report["guard_reason"], "pilot_output_exceeded_bound")
        self.assertEqual(report["failure_phase"], "check_output_size")
        self.assertEqual(report["launcher_output"]["stdout_bytes"], len(stdout))
        self.assertEqual(report["mutation_result"], "not_attempted_read_only")
        self.assertTrue(report["failure_sites"])
        self.assertNotIn("retained_incident_evidence", report)

    def test_apply_retains_smaller_limit_and_unknown_result_is_never_retried(self):
        report, code, process = self.invoke(mode="apply", stdout=self.payload("apply", 3 * 1024 * 1024))
        self.assertEqual(code, 1)
        self.assertEqual(report["launcher_output"]["limit_bytes"], 2 * 1024 * 1024)
        self.assertEqual(report["guard_reason"], "pilot_output_exceeded_bound")
        self.assertEqual(report["mutation_result"], "unknown_requires_report_review")
        self.assertEqual(process.call_count, 1)

    def test_normal_apply_report_keeps_mode_and_success(self):
        report, code, process = self.invoke(mode="apply")
        self.assertEqual(code, 0)
        self.assertFalse(report["read_only"])
        self.assertEqual(report["launcher_output"]["limit_bytes"], 256 * 1024)
        self.assertEqual(process.call_args.args[0][-1], "apply")

    def test_missing_output_reports_lengths_without_exporting_stderr(self):
        report, code, _ = self.invoke(stdout=b"", stderr=b"synthetic-secret-never-export", returncode=1)
        self.assertEqual(code, 1)
        self.assertEqual(report["guard_reason"], "pilot_output_missing")
        self.assertEqual(report["launcher_output"]["stdout_bytes"], 0)
        self.assertEqual(report["launcher_output"]["stderr_bytes"], 29)
        self.assertNotIn("synthetic-secret-never-export", json.dumps(report))

    def test_malformed_output_reports_json_error_without_exporting_body(self):
        report, code, _ = self.invoke(stdout=b"synthetic-secret-never-export")
        self.assertEqual(code, 1)
        self.assertEqual(report["error_type"], "JSONDecodeError")
        self.assertEqual(report["failure_phase"], "parse_report")
        self.assertIsNone(report["guard_reason"])
        self.assertNotIn("synthetic-secret-never-export", json.dumps(report))

    def test_source_hash_mismatch_stops_before_any_child_process(self):
        report, code, process = self.invoke(mode="apply", corrupt_source=True)
        self.assertEqual(code, 1)
        process.assert_not_called()
        self.assertEqual(report["guard_reason"], "source_hash_mismatch")
        self.assertEqual(report["mutation_result"], "not_started")
        self.assertEqual(report["failure_phase"], "validate_and_extract_sources")

    def test_wrong_schema_or_mode_remains_fail_closed(self):
        for data in ([], {"read_only": True, "single_character_pilot": True},
                     {"schema_version": 1, "scan_complete": True,
                      "read_only": False, "single_character_pilot": True}):
            with self.subTest(data=data):
                report, code, _ = self.invoke(stdout=json.dumps(data).encode())
                self.assertEqual(code, 1)
                self.assertEqual(report["guard_reason"], "unexpected_report_schema")
                self.assertEqual(report["failure_phase"], "validate_report")
                self.assertEqual(report["mutation_result"], "not_attempted_read_only")


if __name__ == "__main__":
    unittest.main()
