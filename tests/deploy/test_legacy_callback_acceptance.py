"""Real schema-3 loader/scanner regressions using the saved synthetic report.

Only filesystem ownership, native queries, discovery and log transport are
mocked. Review validation, digest checks, proof and final filtering are real.
"""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ops.deploy import docker_host as dh
from ops.deploy.recovered_logs import ReviewedDockerHost
from tests.deploy.test_callback_producer_proof import analyze_raw
from tests.deploy.test_docker_host import make_config


FIXTURES = Path(__file__).parent / "fixtures"


def legacy_report():
    return json.loads((FIXTURES / "legacy-schema3-report.json").read_text(encoding="utf-8"))


def raw_streams(report):
    """Reconstruct original synthetic bytes and independently check each digest."""
    streams = {}
    prefix = hashlib.sha256(b"abcde").hexdigest()
    for stream in report["logs"]:
        rows = []
        for row in stream["rows"]:
            text = row["text"].replace("session sha256:" + prefix, "session abcde")
            if hashlib.sha256(text.encode()).hexdigest() != row["raw_sha256"]:
                raise AssertionError("Fixture does not bind the original raw line")
            rows.append({"text": text, "at": row["at"]})
        streams[stream["source"]] = rows
    return streams


class LegacyCallbackAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = make_config(self.root)
        (self.config.app_dir / self.config.env_file).write_text("# Synthetic test configuration\n", encoding="ascii")
        (self.config.app_dir / self.config.compose_file).write_text("services: {}\n", encoding="ascii")
        self.report = legacy_report()
        self.streams = raw_streams(self.report)
        self.web = next(name for name in self.streams if name.startswith(self.config.gunicorn_service + "/"))
        self.review = json.loads((FIXTURES / "legacy-schema3-review.json").read_text(encoding="utf-8"))
        self.report_path = self.root / "legacy-report.json"
        self.review_path = self.root / "schema3-review.json"
        self.host = dh.DockerHost(self.config)
        self.host.backup_path = self.root
        self.host.auth_replica_counts = {service: 1 for service in self.config.auth_services}
        self.current = {key: True for key in ("read_only", "assets_successful", "assets_relationships_valid",
                        "identity_and_token_inventory_preserved", "callback_source_verified")}

    def load(self, report=None, schema=3):
        self.report_path.write_text(json.dumps(self.report if report is None else report), encoding="ascii")
        review = deepcopy(self.review)
        review["retained_log_findings"].update(report_path=str(self.report_path),
            report_sha256=hashlib.sha256(self.report_path.read_bytes()).hexdigest())
        review["schema_version"] = schema
        if schema != 3:
            review.pop("retained_log_findings")
        if schema == 4:
            review["semantic_recovery"] = {"report_path": str(self.root / "semantic.json"), "report_sha256": "f" * 64}
        self.review_path.write_text(json.dumps(review), encoding="ascii")
        self.host._load_retained_verifier_review({"attempt_id": review["attempt_id"]}, review["plan_sha256"],
                                               review_path=self.review_path)
        return review

    def asset_only(self):
        report = deepcopy(self.report)
        report["logs"] = [stream for stream in report["logs"] if stream["source"] != self.web]
        report.pop("login_http")
        return report

    def seed_old_callback_acceptance(self, host=None):
        host = self.host if host is None else host
        rows = self.streams[self.web][:11]
        hashes = tuple(hashlib.sha256(row["text"].encode()).hexdigest() for row in rows)
        host.recovered_retained_log_records = {(self.web, hashes[0]): [hashes]}
        host.recovered_retained_log_seen = {(*next(iter(host.recovered_retained_log_records)), hashes): 1}
        return "\n".join(row["text"] for row in rows)

    @contextmanager
    def boundaries(self, streams=None):
        streams = self.streams if streams is None else streams
        identities = {row["service"]: row["container_id"] for row in self.review["containers"]}
        def transport(command, **kwargs):
            for source, rows in streams.items():
                if command[-1] in source.split("/"):
                    yield from (row["text"] for row in rows)
        with mock.patch.object(self.host, "_worker_recycle_private_bytes", side_effect=lambda path, maximum: path.read_bytes()), \
             mock.patch.object(self.host, "_manage_live", side_effect=lambda *args, **kwargs:
                 "BUH_RETAINED_FINDINGS_CURRENT_BEGIN\n" + json.dumps(self.current) +
                 "\nBUH_RETAINED_FINDINGS_CURRENT_END") as native, \
             mock.patch.object(self.host, "_running_service_containers", side_effect=lambda service, **kwargs: [identities[service]]), \
             mock.patch.object(self.host, "_stream_log_lines", side_effect=transport) as logs:
            yield native, logs

    def test_saved_legacy_report_passes_real_loader_but_cannot_clear_callback_at_scan(self):
        self.load()
        self.assertEqual(self.host.retained_verifier_review["schema_version"], 3)
        with self.boundaries() as (native, logs):
            with self.assertRaisesRegex(dh.LogScanError, "Unresolved callback.*callback-operation completion") as caught:
                self.host._scan_new_logs(self.config.auth_services)
            self.assertEqual(caught.exception.findings, (dh.CALLBACK_RECOVERY_UNRESOLVED,))
            self.assertFalse(caught.exception.scan_complete)
            native.assert_not_called()
            logs.assert_not_called()
        self.assertEqual(self.host.recovered_retained_log_records, {})

    def test_same_saved_callback_is_unresolved_in_both_corrected_adapters(self):
        for adapter in ("fresh", "retained"):
            result = analyze_raw({self.web: self.streams[self.web]}, adapter)
            self.assertEqual(result["decision"], ["handled_callback", "unresolved"])
            self.assertEqual(result["summary"]["handled_count"], 0)

    def test_reported_callback_cannot_disappear_when_current_transport_has_no_rows(self):
        self.load()
        with self.boundaries({}), self.assertRaisesRegex(dh.DeploymentError, "Unresolved callback"):
            self.host._scan_new_logs(self.config.auth_services)

    def test_missing_native_header_cannot_discard_the_reported_traceback(self):
        report = deepcopy(self.report)
        report["logs"][1]["rows"].pop(0)
        self.load(report)
        with self.boundaries({}), self.assertRaisesRegex(dh.DeploymentError, "no native header.*incomplete"):
            self.host._scan_new_logs(self.config.auth_services)

    def test_valid_non_callback_legacy_report_still_passes_loader_to_scanner(self):
        report = self.asset_only()
        self.load(report)
        streams = {name: rows for name, rows in self.streams.items() if name != self.web}
        with self.boundaries(streams) as (native, _):
            warnings = self.host._scan_new_logs(self.config.auth_services)
            self.assertEqual(len(warnings), 1)
            self.assertIn("exact historical asset-name Invalid IDs 404", warnings[0])
            self.assertIn("SET TRANSACTION READ ONLY", native.call_args.args[-1])
        self.assertEqual(sum(map(len, self.host.recovered_retained_log_records.values())), 3)
        self.assertFalse(any(source == self.web for source, _ in self.host.recovered_retained_log_records))

    def test_asset_only_report_cannot_hide_a_live_callback_in_fallback_service_scan(self):
        self.load(self.asset_only())
        with self.boundaries(), self.assertRaises(dh.LogScanError) as caught:
            self.host._scan_new_logs(self.config.auth_services)
        self.assertIn("Unresolved callback", "\n".join(caught.exception.findings))

    def test_final_scanner_rejects_matching_old_cached_callback_bytes(self):
        text = self.seed_old_callback_acceptance()
        self.host.recovered_retained_log_seen = {}
        with self.assertRaisesRegex(dh.LogScanError, "Unresolved callback"):
            self.host._classify_log_batch([(self.web, text)], set(), None)
        self.assertEqual(self.host.recovered_retained_log_seen, {})

    def test_old_annotations_and_prefilled_callback_acceptance_cannot_qualify_report(self):
        report = deepcopy(self.report)
        report["callback_recovery_accepted"] = True
        report["authentication_completed"] = True
        for stream in report["logs"]:
            for row in stream["rows"]:
                row.update(session_sha256=["f" * 64], trusted=True, operation_completed=True)
        self.load(report)
        with self.boundaries(), self.assertRaisesRegex(dh.DeploymentError, "Unresolved callback"):
            self.host._scan_new_logs(self.config.auth_services)

    def test_review_reapplication_and_schema_transitions_clear_legacy_acceptance(self):
        for schema in (3, 3, 2, 4):
            self.seed_old_callback_acceptance()
            self.load(schema=schema)
            self.assertEqual(self.host.recovered_retained_log_records, {})
            self.assertEqual(self.host.recovered_retained_log_seen, {})
        self.seed_old_callback_acceptance()
        with mock.patch.object(self.host, "_scan_semantic_logs", return_value=()) as semantic:
            self.assertEqual(self.host._scan_new_logs(self.config.auth_services), ())
            semantic.assert_called_once()
        self.assertEqual(self.host.recovered_retained_log_records, {})
        self.host.retained_verifier_review = None
        text = self.seed_old_callback_acceptance()
        with self.assertRaisesRegex(dh.LogScanError, "Unresolved callback"):
            self.host._classify_log_batch([(self.web, text)], set(), None)

    def test_missing_or_failed_review_load_cannot_leave_cached_acceptance(self):
        for raw in (b"{", b'{"schema_version": false}', None):
            self.seed_old_callback_acceptance()
            if raw is None:
                self.review_path.unlink(missing_ok=True)
                self.host._load_retained_verifier_review({"attempt_id": "gh-123-1"}, "a" * 64, review_path=self.review_path)
            else:
                self.review_path.write_bytes(raw)
                with self.assertRaises(dh.DeploymentError):
                    self.host._load_retained_verifier_review({"attempt_id": "gh-123-1"}, "a" * 64, review_path=self.review_path)
            self.assertEqual(self.host.recovered_retained_log_records, {})

    def test_digest_current_state_and_incomplete_asset_evidence_still_block(self):
        report = self.asset_only()
        for change in ("digest", "current", "incomplete", "conflicting"):
            selected = deepcopy(report)
            if change == "incomplete":
                selected["logs"][0]["complete"] = False
            if change == "conflicting":
                selected["logs"][0]["rows"][0]["text"] = selected["logs"][0]["rows"][0]["text"].replace("ID:9", "ID:99")
            self.load(selected)
            self.seed_old_callback_acceptance()
            if change == "digest":
                self.report_path.write_bytes(self.report_path.read_bytes() + b" ")
            self.current["assets_successful"] = change != "current"
            with self.boundaries(), self.assertRaises(dh.DeploymentError):
                self.host._scan_new_logs(self.config.auth_services)
            self.assertEqual(self.host.recovered_retained_log_records, {})

    def test_partial_conflicting_and_duplicate_callback_records_do_not_reuse_old_acceptance(self):
        text = self.seed_old_callback_acceptance()
        for changed in (text.split("\n")[0], "\n".join(text.split("\n")[:-1]),
                        text.replace("line 29", "line 30"), text + "\n" + text):
            with self.subTest(text=changed[:60]), self.assertRaisesRegex(dh.LogScanError, "Unresolved callback"):
                self.host._classify_log_batch([(self.web, changed)], set(), None)

    def test_rollback_adapter_cannot_prestrip_callback_using_old_review_category(self):
        host = ReviewedDockerHost(self.config)
        host.recovery_baseline_verified = True
        host.recovered_log_review = mock.Mock()
        host.recovered_log_review.classify_web_debug.return_value = "caught-ESI-callback-miss"
        text = self.seed_old_callback_acceptance(host)
        with self.assertRaisesRegex(dh.LogScanError, "Unresolved callback"):
            host._classify_log_batch([(self.config.gunicorn_service, text)], set(), "rollback")
        host.recovered_log_review.classify_web_debug.assert_not_called()


if __name__ == "__main__":
    unittest.main()
