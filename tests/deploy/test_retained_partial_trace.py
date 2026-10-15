"""Selected native evidence survives header loss, quiet logs and old receipts."""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from ops.deploy import docker_host as dh
from ops.deploy.recovered_logs import ReviewedDockerHost
from tests.deploy.test_callback_producer_proof import parse_rows, state
from tests.deploy import test_legacy_callback_acceptance as legacy
from tests.deploy.test_legacy_callback_acceptance import raw_streams


PLACEMENTS = ("leading", "after-normal", "middle")
BOUNDARIES = ("full", "truncated", "frames-only", "terminal-only", "chain-tail", "native-header-only")


def normal_row(message="Received callback for AnonymousUser session abcde", second=0):
    raw = f"[04/Oct/2026 00:00:{second:02d}] DEBUG [esi.views:73] {message}"
    return {"at": f"2026-10-04T00:00:{second:02d}.000Z",
            "text": raw.replace("session abcde", "session sha256:" + hashlib.sha256(b"abcde").hexdigest()),
            "raw_sha256": hashlib.sha256(raw.encode()).hexdigest()}


def native_row(text):
    return {"at": "2026-10-04T00:00:01.000Z", "text": text,
            "raw_sha256": hashlib.sha256(text.encode()).hexdigest()}


class RetainedPartialTraceTests(unittest.TestCase):
    def setUp(self):
        # Reuse the actual digest-bound schema-3 loader fixture, not an analyzer
        # substitute. Only ownership/native query/discovery/transport are mocked.
        self.case = legacy.LegacyCallbackAcceptanceTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.asset_streams = {key: rows for key, rows in self.case.streams.items() if key != self.case.web}

    def fragment_report(self, placement="after-normal", boundary="full"):
        report = deepcopy(self.case.report)
        web = next(stream for stream in report["logs"] if stream["source"] == self.case.web)
        rows = web["rows"]
        fragment = {
            "full": rows[1:11], "truncated": rows[1:4], "frames-only": rows[2:10],
            "terminal-only": rows[10:11], "native-header-only": rows[:1],
            "chain-tail": rows[1:11] + [native_row("During handling of the above exception, another exception occurred:")]
                          + rows[1:3],
        }[boundary]
        prefix = [] if placement == "leading" else [normal_row()]
        if placement == "middle":
            prefix.append(normal_row("Ordinary request metadata"))
        suffix = [normal_row("Ordinary later request", second=2)] if placement == "middle" else []
        web["rows"] = prefix + fragment + suffix
        raw_streams(report)  # Verify every original raw-line digest.
        return report

    def refuse_scan(self, report):
        c = self.case
        c.load(report)
        with c.boundaries(self.asset_streams) as (native, transport):
            with self.assertRaises(dh.DeploymentError) as caught:
                c.host._scan_new_logs(c.config.auth_services)
            native.assert_not_called()
            transport.assert_not_called()
        self.assertEqual(c.host.recovered_retained_log_records, {})
        self.assertEqual(c.host.recovered_retained_log_seen, {})
        if isinstance(caught.exception, dh.LogScanError):
            self.assertFalse(caught.exception.scan_complete)
        return caught.exception

    def cleanup_gate(self):
        """Execute the actual nested gate without importing the POSIX receiver.

        Full main/installation/recovery never executes. The source node is
        compiled unchanged; private reads and atomic receipt writes are isolated.
        """
        path = Path(dh.__file__).with_name("verifier_repair.py")
        gate = next(node for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                    if isinstance(node, ast.FunctionDef) and node.name == "recycle_evidence_gate")
        c, receipts, document = self.case, [], {}
        review = c.host.retained_verifier_review
        namespace = {"report": document, "plan": {"attempt_id": review["attempt_id"]},
                     "assessment": {"hold_sha256": review["plan_sha256"]}, "review": review,
                     "review_path": c.review_path, "digest": lambda raw: hashlib.sha256(raw).hexdigest(),
                     "private_read": lambda file: file.read_bytes(), "json": json,
                     "DeploymentError": dh.DeploymentError,
                     "_atomic_bytes": lambda file, raw, *a, **kw: receipts.append((file.name, json.loads(raw)))}
        exec(compile(ast.Module(body=[gate], type_ignores=[]), str(path), "exec"), namespace)
        return lambda: namespace["recycle_evidence_gate"](c.host), receipts, document

    def test_saved_full_and_truncated_variants_cannot_pass_with_quiet_current_web(self):
        for boundary in ("full", "truncated"):
            with self.subTest(boundary=boundary):
                error = self.refuse_scan(self.fragment_report(boundary=boundary))
                self.assertRegex(str(error), "(?:Unverified|Incomplete) retained native exception evidence")

    def test_placement_and_header_truncation_boundary_matrix(self):
        # All eighteen variants retain a frame, terminal, chain or native
        # failure header. Adding a harmless predecessor never grants recovery.
        for placement in PLACEMENTS:
            for boundary in BOUNDARIES:
                with self.subTest(placement=placement, boundary=boundary):
                    self.refuse_scan(self.fragment_report(placement, boundary))

    def test_incomplete_exception_chains_at_eof_or_next_header_remain_explicit(self):
        for placement in ("after-normal", "middle"):
            for marker in sorted(dh.NATIVE_TRACEBACK_MARKERS):
                report = self.fragment_report(placement)
                web = next(stream for stream in report["logs"] if stream["source"] == self.case.web)
                index = -1 if placement == "middle" else len(web["rows"])
                web["rows"].insert(index, native_row(marker))
                with self.subTest(placement=placement, marker=marker):
                    self.assertIn("Incomplete retained", str(self.refuse_scan(report)))

    def test_missing_header_in_worker_stream_is_also_accounted_for(self):
        report = self.case.asset_only()
        web = self.fragment_report()["logs"][1]
        report["logs"][0]["rows"].extend(web["rows"])
        self.assertIn("Unverified retained", str(self.refuse_scan(report)))

    def test_unqualified_native_error_header_without_trace_is_not_a_clean_record(self):
        for level in ("ERROR", "CRITICAL"):
            report = self.fragment_report(boundary="terminal-only")
            report["logs"][1]["rows"] = [native_row(
                f"[04/Oct/2026 00:00:01] {level} [other.component:1] Operation failed")]
            with self.subTest(level=level):
                self.assertIn("Unverified retained", str(self.refuse_scan(report)))

    def test_fragments_remain_unknown_or_unresolved_through_both_semantic_adapters(self):
        for boundary in BOUNDARIES:
            raw = raw_streams(self.fragment_report(boundary=boundary))[self.case.web]
            for adapter in ("fresh", "retained"):
                with self.subTest(boundary=boundary, adapter=adapter):
                    events = []
                    try:
                        events.extend(parse_rows(raw, self.case.web, adapter, {}))
                    except dh.SemanticRecordError as error:
                        events.append(error.partial_record)
                    with dh.RetainedSemanticAnalyzer(state()) as analyzer:
                        for event in events:
                            analyzer.feed(event)
                        result = analyzer.finish()
                    self.assertGreater(result["unknown_count"] + result["unresolved_count"], 0)
                    self.assertEqual(result["handled_count"], 0)

    def test_stale_matching_partial_record_acceptance_cannot_filter_native_evidence(self):
        c = self.case
        for boundary in BOUNDARIES:
            rows = raw_streams(self.fragment_report(boundary=boundary))[c.web]
            text = "\n".join(row["text"] for row in rows)
            hashes = tuple(hashlib.sha256(row["text"].encode()).hexdigest() for row in rows)
            c.host.recovered_retained_log_records = {(c.web, hashes[0]): [hashes]}
            c.host.recovered_retained_log_seen = {}
            with self.subTest(boundary=boundary), self.assertRaises(dh.LogScanError):
                c.host._classify_log_batch([(c.web, text)], set(), None)
            self.assertEqual(c.host.recovered_retained_log_seen, {})

    def test_valid_report_then_partial_report_and_reapplied_review_cannot_reuse_acceptance(self):
        c = self.case
        c.load(c.asset_only())
        with c.boundaries(self.asset_streams):
            self.assertEqual(len(c.host._scan_new_logs(c.config.auth_services)), 1)
        self.assertEqual(sum(map(len, c.host.recovered_retained_log_records.values())), 3)
        for _ in range(2):
            self.refuse_scan(self.fragment_report())

    def test_schema_transitions_cannot_restore_partial_record_clearance(self):
        c = self.case
        report = self.fragment_report()
        current = raw_streams(report)
        for schema in (3, 2, 4, 3):
            c.seed_old_callback_acceptance()
            c.load(report, schema=schema)
            self.assertEqual(c.host.recovered_retained_log_records, {})
            with self.subTest(schema=schema), c.boundaries(current), self.assertRaises((dh.DeploymentError, OSError)):
                # Schema 4 has no valid private semantic proof in this fixture;
                # that missing proof must block instead of borrowing schema 3.
                c.host._scan_new_logs(c.config.auth_services)

    def test_asset_only_report_cannot_hide_partial_trace_in_fallback_current_scan(self):
        c = self.case
        c.load(c.asset_only())
        with c.boundaries(raw_streams(self.fragment_report())), self.assertRaises(dh.LogScanError) as caught:
            c.host._scan_new_logs(c.config.auth_services)
        self.assertFalse(caught.exception.scan_complete)
        self.assertIn("Unverified native exception fragment", "\n".join(caught.exception.findings))

    def test_rollback_old_review_category_cannot_prestrip_partial_trace(self):
        c = self.case
        host = ReviewedDockerHost(c.config)
        host.recovery_baseline_verified = True
        host.recovered_log_review = mock.Mock()
        host.recovered_log_review.classify_web_debug.return_value = "caught-ESI-callback-miss"
        for boundary in BOUNDARIES:
            rows = raw_streams(self.fragment_report(boundary=boundary))[c.web]
            with self.subTest(boundary=boundary), self.assertRaises(dh.LogScanError):
                host._classify_log_batch([(c.config.gunicorn_service, "\n".join(row["text"] for row in rows))], set(), "rollback")
        for call in host.recovered_log_review.classify_web_debug.call_args_list:
            self.assertFalse(dh._native_exception_evidence(call.args[0]))
            self.assertFalse(dh._callback_failure_body("\n".join(call.args[0])))

    def test_precleanup_rechecks_partial_evidence_without_writing_qualification_receipts(self):
        c = self.case
        old = c.root / "RETAINED-LOG-FINDINGS.json"
        old.write_bytes(b'{"schema_version":1,"callback_recovery_accepted":false}\n')
        for boundary in BOUNDARIES:
            c.load(self.fragment_report(boundary=boundary))
            gate, receipts, document = self.cleanup_gate()
            with self.subTest(boundary=boundary), c.boundaries(self.asset_streams), self.assertRaises(dh.DeploymentError):
                gate()
            self.assertEqual(receipts, [])
            self.assertNotIn("retained_log_findings_before_cleanup", document)
            self.assertEqual(c.host.recovered_retained_log_records, {})
            self.assertEqual(old.read_bytes(), b'{"schema_version":1,"callback_recovery_accepted":false}\n')

    def test_precleanup_rereads_report_after_successful_asset_scan(self):
        c = self.case
        c.load(c.asset_only())
        with c.boundaries(self.asset_streams):
            c.host._scan_new_logs(c.config.auth_services)
        c.load(self.fragment_report())
        gate, receipts, _ = self.cleanup_gate()
        with c.boundaries(self.asset_streams), self.assertRaises(dh.LogScanError):
            gate()
        self.assertEqual(receipts, [])

    def test_valid_assets_and_benign_multiline_client_text_still_qualify(self):
        c = self.case
        report = c.asset_only()
        benign = [normal_row("Received literal client text: CallbackRedirect.DoesNotExist session abcde"),
                  native_row('    quoted "Traceback (most recent call last):"'),
                  native_row('    client value: "ValueError: ordinary quoted payload"'),
                  native_row("Status: ready"), native_row("    legitimate multiline message"),
                  normal_row("Ordinary native header without a traceback", second=2)]
        report["logs"].append({**deepcopy(c.report["logs"][1]), "rows": benign})
        c.load(report)
        with c.boundaries(self.asset_streams):
            self.assertEqual(len(c.host._scan_new_logs(c.config.auth_services)), 1)
            gate, receipts, document = self.cleanup_gate()
            gate()
        self.assertEqual(sum(map(len, c.host.recovered_retained_log_records.values())), 3)
        self.assertEqual([name for name, _ in receipts], ["WORKER-RECYCLES.json", "RETAINED-LOG-FINDINGS.json"])
        self.assertEqual(len(receipts[-1][1]["records"]), 3)
        self.assertIn("retained_log_findings_before_cleanup", document)
        self.assertFalse(dh._native_exception_evidence(row["text"] for row in benign))

    def test_extra_exception_tail_cannot_borrow_a_valid_asset_records_recovery(self):
        for tail in (["Traceback (most recent call last):", "aiopenapi3.errors.HTTPError"],
                     ["aiopenapi3.errors.HTTPError"],
                     ["Traceback (most recent call last):", "esi.models.CallbackRedirect.DoesNotExist: synthetic"]):
            report = self.case.asset_only()
            first_stop = next(index for index, row in enumerate(report["logs"][0]["rows"][1:], 1)
                              if dh.CELERY_LOG_HEADER_RE.match(row["text"]))
            report["logs"][0]["rows"][first_stop:first_stop] = [native_row(line) for line in tail]
            with self.subTest(tail=tail):
                self.refuse_scan(report)

    def test_partial_native_header_text_does_not_hide_following_frames(self):
        report = self.fragment_report(boundary="frames-only")
        report["logs"][1]["rows"].insert(1, native_row("[04/Oct/2026 00:00:01] DEBUG [esi.decorators:"))
        self.assertIn("Unverified retained", str(self.refuse_scan(report)))

    def test_cut_native_frame_without_opener_or_terminal_remains_unverified(self):
        for fragment in ('  File "/installed/esi/decorators.py', '  File "/installed/esi/decorators.py", line 29, in _check_'):
            report = self.fragment_report()
            report["logs"][1]["rows"] = [normal_row(), native_row(fragment)]
            with self.subTest(fragment=fragment):
                self.assertIn("Unverified retained", str(self.refuse_scan(report)))

    def test_malformed_physical_rows_cannot_hide_structural_evidence(self):
        for change in ("newline", "replacement", "clock"):
            report = self.fragment_report()
            web = report["logs"][1]
            if change == "newline":
                web["rows"] = [native_row(normal_row()["text"] + "\nTraceback (most recent call last):")]
            elif change == "replacement":
                web["rows"] = [native_row(normal_row()["text"] + "\ufffd")]
            else:
                web["rows"][0]["at"] = None
            with self.subTest(change=change):
                self.refuse_scan(report)


if __name__ == "__main__":
    unittest.main()
