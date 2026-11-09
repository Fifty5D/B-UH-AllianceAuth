"""Complete-record qualification through real loaders, filters and receipt gates."""
from copy import deepcopy
from datetime import datetime, timezone
import traceback
import unittest

from ops.deploy import docker_host as dh
from ops.deploy.recovered_logs import RecoveredLogReview, ReviewedDockerHost, TASK_CLOCKS, http_traceback
from tests.deploy import test_retained_partial_trace as partial
from tests.deploy import test_semantic_recovery as semantic
from tests.deploy.test_callback_producer_proof import parse_rows, state
from tests.deploy.test_legacy_callback_acceptance import raw_streams


FRAGMENTS = {
    "frame": '  File "/synthetic/unrelated.py", line 1, in unrelated',
    "cut-path": '  File "/synthetic/unrelated.py',
    "cut-function": '  File "/synthetic/unrelated.py", line 1, in ',
    "cut-function-space": '  File "/synthetic/unrelated.py", line 1, in \t ',
    "terminal": "HTTPError",
    "other-terminal": "RuntimeError: unrelated synthetic exception",
    "chain": "During handling of the above exception, another exception occurred:",
}


def http_record():
    return ["[2026-10-01 11:00:00,123: ERROR/MainProcess] Task "
            "structures.tasks.update_structures_assets_for_owner"
            "[aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee] raised unexpected: HTTPError()",
            "Traceback (most recent call last):", '  File "/synthetic/tasks.py", line 8, in update',
            "    raise HTTPError()", "HTTPError"]


class CompleteRetainedTracebackTests(unittest.TestCase):
    def setUp(self):
        self.harness = partial.RetainedPartialTraceTests()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.case = self.harness.case

    def asset_variant(self, fragment, placement, boundary):
        report = self.case.asset_only()
        rows = report["logs"][0]["rows"]
        stop = (next(i for i, row in enumerate(rows[1:], 1) if dh.CELERY_LOG_HEADER_RE.match(row["text"]))
                if boundary == "next-header" else len(rows))
        index = stop if placement == "after-terminal" else stop - 1
        rows.insert(index, partial.native_row(fragment))
        raw_streams(report)
        return report

    def test_asset_fragment_matrix_refuses_loader_scanner_and_receipts(self):
        c = self.case
        for name, fragment in FRAGMENTS.items():
            for placement in ("before-terminal", "after-terminal"):
                for boundary in ("eof", "next-header"):
                    report = self.asset_variant(fragment, placement, boundary)
                    for current in ({}, raw_streams(report)):
                        with self.subTest(fragment=name, placement=placement, boundary=boundary, quiet=not current):
                            c.load(report)
                            with c.boundaries(current) as (native, transport):
                                with self.assertRaises(dh.DeploymentError) as caught:
                                    c.host._scan_new_logs(c.config.auth_services)
                                if isinstance(caught.exception, dh.LogScanError):
                                    self.assertFalse(caught.exception.scan_complete)
                                native.assert_not_called()
                                transport.assert_not_called()
                                gate, receipts, document = self.harness.cleanup_gate()
                                with self.assertRaises(dh.DeploymentError):
                                    gate()
                            self.assertEqual(c.host.recovered_retained_log_records, {})
                            self.assertEqual(c.host.recovered_retained_log_seen, {})
                            self.assertEqual(receipts, [])
                            self.assertNotIn("retained_log_findings_before_cleanup", document)

    def test_matching_stale_asset_cache_cannot_filter_any_fragment(self):
        c = self.case
        for name, fragment in FRAGMENTS.items():
            for placement in ("before-terminal", "after-terminal"):
                report = self.asset_variant(fragment, placement, "next-header")
                stream = report["logs"][0]
                records = list(dh._retained_log_records(stream["rows"]))
                c.host.recovered_retained_log_records = {}
                c.host.recovered_retained_log_seen = {}
                for record in records:
                    hashes = tuple(row["raw_sha256"] for row in record)
                    c.host.recovered_retained_log_records.setdefault((stream["source"], hashes[0]), []).append(hashes)
                first = tuple(row["raw_sha256"] for row in records[0])
                text = "\n".join(row["text"] for row in raw_streams(report)[stream["source"]])
                with self.subTest(fragment=name, placement=placement), self.assertRaises(dh.LogScanError) as caught:
                    c.host._classify_log_batch([(stream["source"], text)], set(), None)
                self.assertFalse(caught.exception.scan_complete)
                self.assertNotIn((stream["source"], first[0], first), c.host.recovered_retained_log_seen)

    def test_report_schema_reuse_and_fallback_cannot_regrant_fragment_acceptance(self):
        c = self.case
        report = self.asset_variant(FRAGMENTS["cut-path"], "after-terminal", "next-header")
        streams = raw_streams(report)
        (c.root / "semantic.json").write_bytes(b"{}")
        for schema in (3, 2, 4, 3):
            c.load(c.asset_only())
            with c.boundaries(self.harness.asset_streams):
                c.host._scan_new_logs(c.config.auth_services)
            c.load(report, schema=schema)
            self.assertEqual(c.host.recovered_retained_log_records, {})
            with self.subTest(schema=schema), c.boundaries(streams), self.assertRaises(dh.DeploymentError):
                c.host._scan_new_logs(c.config.auth_services)
            self.assertEqual(c.host.recovered_retained_log_records, {})
        # No retained proof/cache: the final current scanner still keeps the tail.
        c.load(schema=2)
        with c.boundaries(streams), self.assertRaises(dh.LogScanError) as caught:
            c.host._scan_new_logs(c.config.auth_services)
        self.assertFalse(caught.exception.scan_complete)

    def test_precleanup_rereads_changed_asset_report_and_preserves_old_receipt(self):
        c = self.case
        old = c.root / "RETAINED-LOG-FINDINGS.json"
        old.write_bytes(b'{"schema_version":1,"historical":true}\n')
        c.load(c.asset_only())
        with c.boundaries(self.harness.asset_streams):
            c.host._scan_new_logs(c.config.auth_services)
        c.load(self.asset_variant(FRAGMENTS["cut-function"], "after-terminal", "next-header"))
        gate, receipts, _ = self.harness.cleanup_gate()
        with c.boundaries({}), self.assertRaises(dh.LogScanError):
            gate()
        self.assertEqual(receipts, [])
        self.assertEqual(c.host.recovered_retained_log_records, {})
        self.assertEqual(old.read_bytes(), b'{"schema_version":1,"historical":true}\n')

    def test_literal_message_frame_cannot_supply_legacy_asset_proof_or_cache(self):
        c = self.case
        for prefix in ("    literal client text: ", "literal message says: "):
            report = c.asset_only()
            stream = report["logs"][0]
            stream["rows"][2] = partial.native_row(prefix + stream["rows"][2]["text"])
            streams = raw_streams(report)
            for current in ({}, streams):
                with self.subTest(prefix=prefix, quiet=not current):
                    c.load(report)
                    with c.boundaries(current) as (native, logs), self.assertRaises(dh.DeploymentError):
                        c.host._scan_new_logs(c.config.auth_services)
                    native.assert_not_called()
                    logs.assert_not_called()
                    self.assertEqual(c.host.recovered_retained_log_records, {})
                    gate, receipts, _ = self.harness.cleanup_gate()
                    with c.boundaries(current), self.assertRaises(dh.DeploymentError):
                        gate()
                    self.assertEqual(receipts, [])
            c.host.recovered_retained_log_records = {}
            c.host.recovered_retained_log_seen = {}
            for record in dh._retained_log_records(stream["rows"]):
                hashes = tuple(row["raw_sha256"] for row in record)
                c.host.recovered_retained_log_records.setdefault((stream["source"], hashes[0]), []).append(hashes)
            with self.assertRaises(dh.LogScanError):
                c.host._classify_log_batch([(stream["source"], "\n".join(row["text"] for row in streams[stream["source"]]))], set(), None)

    def test_literal_message_frame_cannot_supply_semantic_provider_or_asset_proof(self):
        fixture = semantic.ProviderRecoveryTests().fixture
        records, before, current = fixture()
        self.assertTrue(dh.prove_systemic_provider_recovery(records, semantic.SOURCES, before, current, complete=True))
        asset = semantic.AssetRecoveryTests().asset_case()
        self.assertTrue(dh.prove_semantic_recovery(asset, semantic.SOURCES))
        for frame_index in (2, 4):
            for prefix in ("    literal client text: ", "literal message says: "):
                records, before, current = fixture()
                records[0]["rows"][frame_index]["text"] = prefix + records[0]["rows"][frame_index]["text"]
                with self.subTest(frame=frame_index, prefix=prefix):
                    self.assertFalse(dh.prove_systemic_provider_recovery(records, semantic.SOURCES, before, current, complete=True))
                    evidence = {"sources": semantic.SOURCES, "inventory_before": before, "owners": current,
                                "observed_at": "2026-01-01T00:03:00Z", "identity_preserved": True, "credentials_valid": True}
                    with dh.RetainedSemanticAnalyzer(evidence) as analyzer:
                        for record in records:
                            analyzer.feed(record)
                        result = analyzer.finish()
                    self.assertEqual(result["recovered_count"], 0)
                    self.assertGreater(result["unresolved_count"] + result["unknown_count"], 0)
                other = deepcopy(asset)
                index = next(i for i, row in enumerate(other["record"]["rows"])
                             if row["text"].startswith('  File "/installed/esi/openapi_clients.py'))
                other["record"]["rows"][index]["text"] = prefix + other["record"]["rows"][index]["text"]
                self.assertFalse(dh.prove_semantic_recovery(other, semantic.SOURCES))

    def test_both_semantic_adapters_and_normalized_reuse_preserve_incomplete_frames(self):
        c = self.case
        source = c.asset_only()["logs"][0]["source"]
        original = raw_streams(c.asset_only())[source]
        for adapter in ("fresh", "retained"):
            valid = list(parse_rows(original, source, adapter, {}))
            self.assertEqual(len(valid), 3)
            for name in ("cut-path", "cut-function", "chain", "terminal"):
                for placement in ("before-terminal", "after-terminal"):
                    rows = raw_streams(self.asset_variant(FRAGMENTS[name], placement, "next-header"))[source]
                    stats, records = {}, []
                    try:
                        records.extend(parse_rows(rows, source, adapter, stats))
                    except dh.SemanticRecordError as error:
                        records.append(error.partial_record)
                        self.assertFalse(stats["read_complete"])
                    with self.subTest(adapter=adapter, fragment=name, placement=placement), dh.RetainedSemanticAnalyzer(state()) as analyzer:
                        for record in records:
                            analyzer.feed(record)
                        result = analyzer.finish()
                    self.assertGreaterEqual(sum(len(record["rows"]) for record in records), len(valid[0]["rows"]) + 1)
                    self.assertFalse(result["analysis_complete"])
                    self.assertGreater(result["unknown_count"], 0)
                    self.assertEqual(result["recovered_count"], 0)
            # A pre-normalized old record cannot bypass the shared body validator.
            record = deepcopy(valid[0])
            record["rows"].append(partial.native_row(FRAGMENTS["cut-path"]))
            with dh.RetainedSemanticAnalyzer(state()) as analyzer:
                analyzer.feed(record)
                result = analyzer.finish()
            self.assertFalse(result["analysis_complete"])
            self.assertGreater(result["unknown_count"], 0)
            self.assertEqual(result["recovered_count"], 0)

    def rollback_host(self):
        host = ReviewedDockerHost(self.case.config)
        host.recovery_baseline_verified = True
        host.reviewed_worker_ids = frozenset({"000000000001"})
        # Synthetic counterparts of the existing complete-snapshot fixture's
        # clocks and aliases. Its validation integration remains covered in Ubuntu.
        status = {model: [{field: "2026-10-01T11:55:00+00:00" for field in
                          {clock for name, clock in TASK_CLOCKS.values() if name == model}}]
                  for model in {name for name, _ in TASK_CLOCKS.values()}}
        status.update(asset_status=[{"character_id": 123, "run_finished_at": "2026-10-01T11:55:00+00:00"}],
                      asset_characters=[{"pk": 123, "is_disabled": False, "eve_character__character_id": 90000001}])
        host.recovered_log_review = RecoveredLogReview(status, {"asset_recoveries": []},
                                                      started_at=datetime(2026, 10, 1, 12, tzinfo=timezone.utc))
        return host

    def test_rollback_prefilter_final_scanner_accounts_for_fragment_matrix(self):
        for name, fragment in FRAGMENTS.items():
            for placement in ("before-terminal", "after-terminal"):
                for boundary in ("eof", "next-header"):
                    host = self.rollback_host()
                    source = host.config.worker_service + "/000000000001"
                    lines = http_record()
                    lines.insert(len(lines) if placement == "after-terminal" else len(lines) - 1, fragment)
                    if boundary == "next-header":
                        lines.append("[2026-10-01 11:01:00,123: INFO/MainProcess] Ordinary following record")
                    with self.subTest(fragment=name, placement=placement, boundary=boundary):
                        # A complete frame INSIDE the original open trace is
                        # legitimate stack formatting. It has no new terminal.
                        if name == "frame" and placement == "before-terminal":
                            self.assertTrue(host._classify_log_batch([(source, "\n".join(lines))], {host.config.worker_service}, "rollback"))
                        else:
                            with self.assertRaises(dh.LogScanError):
                                host._classify_log_batch([(source, "\n".join(lines))], {host.config.worker_service}, "rollback")
                            self.assertEqual(host.recovered_log_review.summary(), [])

    def test_opened_http_trace_without_frame_stays_unverified_through_final_scanner(self):
        for boundary in ("eof", "next-header"):
            host = self.rollback_host()
            source = host.config.worker_service + "/000000000001"
            lines = [line for line in http_record() if not line.startswith('  File "')]
            self.assertFalse(http_traceback(lines))
            if boundary == "next-header":
                lines.append("[2026-10-01 11:01:00,123: INFO/MainProcess] Ordinary following record")
            with self.subTest(boundary=boundary), self.assertRaises(dh.LogScanError) as caught:
                host._classify_log_batch([(source, "\n".join(lines))], {host.config.worker_service}, "rollback")
            self.assertFalse(caught.exception.scan_complete)
            self.assertIn("Unverified native exception fragment", str(caught.exception))
            self.assertIn("structures.tasks.update_structures_assets_for_owner", str(caught.exception))
            self.assertEqual(host.recovered_log_review.summary(), [])
            for adapter in ("fresh", "retained"):
                rows = [partial.native_row(line) for line in lines]
                records = list(parse_rows(rows, source, adapter, {}))
                self.assertEqual(sum(len(record["rows"]) for record in records), len(rows))
                with self.subTest(boundary=boundary, adapter=adapter), dh.RetainedSemanticAnalyzer(state()) as analyzer:
                    for record in records:
                        analyzer.feed(record)
                    result = analyzer.finish()
                self.assertFalse(result["analysis_complete"])
                self.assertGreater(result["unknown_count"], 0)
                self.assertEqual(result["recovered_count"], 0)

    def test_valid_asset_http_chain_and_literal_multiline_controls(self):
        c = self.case
        c.load(c.asset_only())
        with c.boundaries(self.harness.asset_streams):
            self.assertEqual(len(c.host._scan_new_logs(c.config.auth_services)), 1)
            gate, receipts, _ = self.harness.cleanup_gate()
            gate()
        self.assertEqual(sum(map(len, c.host.recovered_retained_log_records.values())), 3)
        self.assertEqual(len(receipts), 2)
        host = self.rollback_host()
        source = host.config.worker_service + "/000000000001"
        lines = http_record()
        chain = ["", "The above exception was the direct cause of the following exception:", "",
                 "Traceback (most recent call last):", '  File "/synthetic/tasks.py", line 9, in update',
                 "esi.exceptions.HTTPClientError"]
        whitespace_markers = [line + " \t" if line == "Traceback (most recent call last):" else line for line in lines]
        for record in (lines, lines + chain, whitespace_markers,
                       lines + ['    literal client text: "Traceback (most recent call last):"']):
            self.assertTrue(http_traceback(record))
            self.assertTrue(host._classify_log_batch([(source, "\n".join(record))], {host.config.worker_service}, "rollback"))
        # Render the existing admitted class name without adding a requests
        # dependency to the host-independent deployment test environment.
        HTTPError = type("HTTPError", (Exception,), {"__module__": "requests.exceptions"})
        try:
            raise HTTPError("outer synthetic exception") from HTTPError("unraised synthetic cause")
        except HTTPError as error:
            formatted = "".join(traceback.format_exception(error)).splitlines()
        linked = [lines[0], *formatted]
        self.assertTrue(http_traceback(linked))
        self.assertTrue(host._classify_log_batch([(source, "\n".join(linked))], {host.config.worker_service}, "rollback"))
        with self.assertRaises(dh.LogScanError):
            host._classify_log_batch([(source, "\n".join([*linked, FRAGMENTS["cut-path"]]))], {host.config.worker_service}, "rollback")
        self.harness.test_valid_assets_and_benign_multiline_client_text_still_qualify()


if __name__ == "__main__":
    unittest.main()
