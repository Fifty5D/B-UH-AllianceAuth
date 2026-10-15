"""Production-format raw regressions; synthetic inputs, pinned producer source.

The source is inspected as text, never imported/executed. Abstract manually
annotated callback fixtures live separately and are not completion evidence.
"""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
import unittest

from ops.deploy import docker_host as dh
from tests.deploy.test_semantic_recovery import SOURCES, source


FIXTURES = Path(__file__).parent / "fixtures"
WORKER = "worker/" + "a" * 64
PROXY = "nginx/" + "b" * 64


def raw_cases():
    return json.loads((FIXTURES / "callback-raw-review-cases.json").read_text(encoding="utf-8"))


def state():
    sources = deepcopy(SOURCES)
    text = (FIXTURES / "esi-9.6.0-callback-producer.txt").read_text(encoding="utf-8")
    sources["esi.decorators"] = source(text, "esi.decorators")
    return {"sources": sources, "inventory_before": [], "owners": [],
            "observed_at": "2026-01-01T00:05:00Z", "identity_preserved": True,
            "credentials_valid": True, "proxy_service": "nginx"}


def parse_rows(rows, name, adapter, stats):
    if adapter == "fresh":
        return dh._semantic_log_records((row["at"] + " " + row["text"] for row in rows), name,
                   stats=stats, deadline=time.monotonic() + 5)
    return dh._semantic_records_from_rows(deepcopy(rows), name,
                   stats=stats, deadline=time.monotonic() + 5)


def analyze_raw(raw, adapter):
    events, statistics = [], {}
    for name, rows in raw.items():
        statistics[name] = {}
        events.extend(parse_rows(rows, name, adapter, statistics[name]))
    with dh.RetainedSemanticAnalyzer(state()) as analyzer:
        for event in events:
            analyzer.feed(event)
        decision = analyzer._callback_decision(events[0])
        summary = analyzer.finish()
    return {"decision": list(decision) if decision else None, "summary": summary,
            "events": events, "stats": statistics}


class CallbackProducerProofTests(unittest.TestCase):
    def test_review_raw_scenarios_are_unproven_through_both_real_adapters(self):
        for item in raw_cases():
            fresh = analyze_raw(item["raw"], "fresh")
            retained = analyze_raw(item["raw"], "retained")
            with self.subTest(case=item["name"]):
                self.assertEqual(fresh, retained)
                self.assertEqual(fresh["decision"], item["expected"])
                self.assertEqual(fresh["summary"]["handled_count"], 0)
                self.assertGreater(fresh["summary"]["unresolved_count"], 0)

    def test_pinned_producer_logs_partial_identity_and_precedes_view_execution(self):
        text = state()["sources"]["esi.decorators"]["text"]
        tree = ast.parse(text)
        prefix = [n for n in ast.walk(tree) if isinstance(n, ast.Subscript)
                  and ast.unparse(n.value) == "request.session.session_key"]
        self.assertTrue(prefix)
        self.assertTrue(all(isinstance(n.slice, ast.Slice) and n.slice.upper.value == 5 for n in prefix))
        returned = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and n.args
                        and isinstance(n.args[0], ast.Constant) and n.args[0].value ==
                        "Got new token from %s session %s. Returning to view.")
        later = [n for n in ast.walk(tree) if isinstance(n, ast.Return) and isinstance(n.value, ast.Call)
                 and ast.unparse(n.value.func) == "view_func" and n.lineno > returned.lineno]
        self.assertTrue(later)
        self.assertLess(returned.lineno, min(n.lineno for n in later))
        failure = analyze_raw(raw_cases()[0]["raw"], "fresh")["events"][0]
        self.assertTrue(dh._semantic_callback_shape(failure, state()["sources"]))
        self.assertEqual(analyze_raw(raw_cases()[0]["raw"], "fresh")["decision"],
                         ["handled_callback", "unresolved"])

    def test_client_marker_placements_never_become_identity_or_application_failure(self):
        for field in ("agent", "referer", "path"):
            for marker in ("session abcde", "session abcde session vwxyz", "session sha256:" + "f" * 64):
                values = {"agent": "SyntheticBrowser", "referer": "-", "path": "/api/ping/"}
                values[field] = ("/api/ping/?q=" + marker.replace(" ", "%20")) if field == "path" else marker
                raw = {PROXY: [{"at": "2026-01-01T00:02:03Z", "text":
                       f'192.0.2.10 - - [01/Jan/2026:00:02:03 +0000] "GET {values["path"]} HTTP/1.1" '
                       f'200 0 "{values["referer"]}" "{values["agent"]}"'}]}
                for adapter in ("fresh", "retained"):
                    with self.subTest(field=field, marker=marker, adapter=adapter):
                        result = analyze_raw(raw, adapter)
                        event = result["events"][0]
                        self.assertIsNone(dh._semantic_record_session(event))
                        self.assertEqual(event["rows"][0]["session_sha256"], [])
                        self.assertEqual(result["summary"]["unknown_count"], 0)
                        self.assertEqual(result["summary"]["unresolved_count"], 0)
                        self.assertTrue(result["summary"]["analysis_complete"])

    def test_native_prefix_is_retained_as_partial_diagnostic_never_unique_identity(self):
        for adapter in ("fresh", "retained"):
            result = analyze_raw(raw_cases()[0]["raw"], adapter)
            failure = result["events"][0]
            self.assertIsNone(dh._semantic_record_session(failure))
            self.assertEqual(failure["rows"][0]["session_prefix_sha256"], [hashlib.sha256(b"abcde").hexdigest()])
            self.assertEqual(failure["rows"][0]["session_sha256"], [])
            self.assertNotIn("session abcde", str(result["events"]))

    def test_supplied_legacy_annotations_cannot_upgrade_payload_to_proof(self):
        raw = deepcopy(raw_cases()[0]["raw"])
        for rows in raw.values():
            for row in rows:
                row["session_sha256"] = ["f" * 64]
                row["session_prefix_sha256"] = ["f" * 64]
                row["trusted"] = True
        result = analyze_raw(raw, "retained")
        self.assertEqual(result["decision"], ["handled_callback", "unresolved"])
        for event in result["events"]:
            self.assertIsNone(dh._semantic_record_session(event))

    def test_concurrent_native_sessions_remain_separate_unproven_failures(self):
        raw = deepcopy(raw_cases()[1]["raw"])
        other = "worker/" + "c" * 64
        raw[other] = [{**row, "text": row["text"].replace("session abcde", "session vwxyz")}
                      for row in raw[WORKER]]
        raw[PROXY] += [{**row, "text": row["text"].replace("session abcde", "session vwxyz")}
                       for row in list(raw[PROXY])]
        for adapter in ("fresh", "retained"):
            with self.subTest(adapter=adapter):
                result = analyze_raw(raw, adapter)
                summary = result["summary"]
                self.assertEqual(summary["handled_count"], 0)
                self.assertEqual(summary["unresolved_count"], 2)
                self.assertEqual(summary["unknown_count"], 0)
                self.assertTrue(summary["analysis_complete"])
                prefixes = {value for event in result["events"] for row in event["rows"]
                            for value in row["session_prefix_sha256"]}
                self.assertEqual(prefixes, {hashlib.sha256(x).hexdigest() for x in (b"abcde", b"vwxyz")})

    def test_raw_duplicate_views_are_counted_without_creating_completion(self):
        for adapter in ("fresh", "retained"):
            events = analyze_raw(raw_cases()[0]["raw"], adapter)["events"]
            with self.subTest(adapter=adapter), dh.RetainedSemanticAnalyzer(state()) as analyzer:
                for event in events + deepcopy(events):
                    analyzer.feed(event)
                summary = analyzer.finish()
                self.assertEqual(summary["exact_duplicate_physical_views"], len(events))
                self.assertEqual(summary["unresolved_count"], 1)
                self.assertEqual(summary["handled_count"], 0)
                self.assertTrue(summary["analysis_complete"])

    def test_conflicting_normalized_view_of_raw_record_blocks_analysis(self):
        for adapter in ("fresh", "retained"):
            event = analyze_raw(raw_cases()[0]["raw"], adapter)["events"][0]
            conflict = deepcopy(event)
            conflict["rows"][0]["text"] += " changed-content"
            with self.subTest(adapter=adapter), dh.RetainedSemanticAnalyzer(state()) as analyzer:
                analyzer.feed(event)
                analyzer.feed(conflict)
                summary = analyzer.finish()
                self.assertFalse(summary["analysis_complete"])
                self.assertGreater(summary["unknown_count"], 0)
                self.assertEqual(summary["handled_count"], 0)

    def test_raw_normalization_preserves_only_partial_prefix_and_is_idempotent(self):
        for adapter in ("fresh", "retained"):
            original = analyze_raw(raw_cases()[0]["raw"], adapter)
            normalized = {name: [row for event in original["events"] if event["source"] == name
                                for row in event["rows"]] for name in raw_cases()[0]["raw"]}
            replay = analyze_raw(normalized, "retained")
            self.assertEqual(original, replay)
            self.assertTrue(all(dh._semantic_record_session(event) is None for event in replay["events"]))

    def test_raw_truncated_causal_tail_at_eof_or_next_header_blocks_analysis(self):
        raw = raw_cases()[0]["raw"][WORKER]
        for adapter in ("fresh", "retained"):
            for following in ([], [raw[-1]]):
                statistics = {}
                with self.subTest(adapter=adapter, following=bool(following)):
                    with self.assertRaises(dh.SemanticRecordError) as caught:
                        list(parse_rows(raw[:8] + following, WORKER, adapter, statistics))
                    self.assertFalse(statistics["read_complete"])
                    self.assertFalse(caught.exception.partial_record["record_complete"])
                    with dh.RetainedSemanticAnalyzer(state()) as analyzer:
                        analyzer.feed(caught.exception.partial_record)
                        summary = analyzer.finish()
                        self.assertFalse(summary["analysis_complete"])
                        self.assertEqual(summary["handled_count"], 0)

    def test_native_caught_failure_cannot_disappear_when_entire_traceback_is_missing(self):
        for following in (False, True):
            raw = deepcopy(raw_cases()[0]["raw"])
            raw[WORKER] = raw[WORKER][:1] + (raw[WORKER][9:] if following else [])
            if not following:
                raw.pop(PROXY)
            for adapter in ("fresh", "retained"):
                with self.subTest(adapter=adapter, following=following):
                    result = analyze_raw(raw, adapter)
                    self.assertEqual(result["decision"], ["handled_callback", "unresolved"])
                    self.assertEqual(result["summary"]["unresolved_count"], 1)
                    self.assertEqual(result["summary"]["handled_count"], 0)

    def test_raw_causal_separator_without_its_exception_tail_is_incomplete(self):
        original = raw_cases()[0]["raw"][WORKER]
        for separator in (
            "During handling of the above exception, another exception occurred:",
            "The above exception was the direct cause of the following exception:",
        ):
            rows = original[:9] + [{"at": original[0]["at"], "text": separator}]
            for adapter in ("fresh", "retained"):
                for following in ([], [original[-1]]):
                    statistics = {}
                    with self.subTest(separator=separator, adapter=adapter, following=bool(following)):
                        with self.assertRaises(dh.SemanticRecordError) as caught:
                            list(parse_rows(rows + following, WORKER, adapter, statistics))
                        self.assertFalse(statistics["read_complete"])
                        self.assertFalse(caught.exception.partial_record["record_complete"])
                        self.assertEqual(caught.exception.partial_record["rows"][-1]["text"], separator)
                complete = rows + [{"at": original[0]["at"], "text": text} for text in (
                    "", "Traceback (most recent call last):",
                    '  File "/synthetic/view.py", line 1, in view', "RuntimeError: later failure")]
                statistics = {}
                framed = list(parse_rows(complete, WORKER, adapter, statistics))
                self.assertTrue(statistics["read_complete"])
                self.assertTrue(framed[0]["record_complete"])
                self.assertIn(separator, str(framed[0]))

    def test_raw_terminal_and_later_failure_share_complete_causal_frame(self):
        raw = deepcopy(raw_cases()[0]["raw"])
        raw[WORKER].insert(9, {"at": raw[WORKER][0]["at"], "text": "RuntimeError: trailing failure"})
        for adapter in ("fresh", "retained"):
            result = analyze_raw(raw, adapter)
            self.assertIn("trailing failure", result["events"][0]["rows"][-1]["text"])
            self.assertEqual(result["summary"]["handled_count"], 0)
            self.assertGreater(result["summary"]["unresolved_count"], 0)

    def test_raw_record_and_preprocessing_bounds_retain_partial_failure(self):
        failure = raw_cases()[0]["raw"][WORKER][:8]
        for tail in (
            [{"at": failure[0]["at"], "text": " " + "x" * dh.MAX_SEMANTIC_ROW_BYTES}],
            [{"at": failure[0]["at"], "text": "  frame"}] * dh.MAX_SEMANTIC_RECORD_LINES,
        ):
            for adapter in ("fresh", "retained"):
                statistics = {}
                with self.subTest(adapter=adapter, rows=len(tail)):
                    with self.assertRaises(dh.SemanticRecordError) as caught:
                        list(parse_rows(failure + tail, WORKER, adapter, statistics))
                    self.assertTrue(statistics["record_limit_exceeded"])
                    self.assertFalse(caught.exception.partial_record["record_complete"])

    def test_unsupported_metadata_on_harmless_access_record_is_not_a_failure(self):
        for annotation in ("not-an-identity", [None], ["f" * 64, "e" * 64], True):
            raw = {PROXY: [deepcopy(raw_cases()[0]["raw"][PROXY][-1])]}
            raw[PROXY][0]["session_sha256"] = annotation
            result = analyze_raw(raw, "retained")
            self.assertIsNone(dh._semantic_record_session(result["events"][0]))
            self.assertTrue(result["summary"]["analysis_complete"])
            self.assertEqual(result["summary"]["unknown_count"], 0)
            self.assertEqual(result["summary"]["unresolved_count"], 0)

    def test_retained_prefix_metadata_can_only_narrow_visible_partial_attribution(self):
        native = raw_cases()[0]["raw"][WORKER][0]
        for previous in ([], ["f" * 64], True, "not-a-list"):
            rows = [{**native, "session_prefix_sha256": previous}]
            normalized = list(parse_rows(rows, WORKER, "retained", {}))[0]
            self.assertEqual(normalized["rows"][0]["session_prefix_sha256"], [])
            self.assertIsNone(dh._semantic_record_session(normalized))
        access = raw_cases()[1]["raw"][PROXY][-1]
        rows = [{**access, "session_prefix_sha256": [hashlib.sha256(b"abcde").hexdigest()]}]
        normalized = list(parse_rows(rows, PROXY, "retained", {}))[0]
        self.assertEqual(normalized["rows"][0]["session_prefix_sha256"], [])

    def test_marker_inside_redirect_payload_is_not_attributed_to_native_field(self):
        for message in (
            "Processed callback for AnonymousUser session abcde. Redirecting to /next/?q=session vwxyz. Redirecting to /api/",
            "No callback for user session vwxyz session abcde",
        ):
            row = {"at": "2026-01-01T00:01:00Z", "text":
                   "[01/Jan/2026 00:01:00] DEBUG [esi.views:91] " + message}
            self.assertEqual(dh._semantic_session_prefix(row["text"]), [])
            for adapter in ("fresh", "retained"):
                result = analyze_raw({WORKER: [row]}, adapter)
                self.assertIsNone(dh._semantic_record_session(result["events"][0]))
                self.assertEqual(result["summary"]["unknown_count"], 0)


if __name__ == "__main__":
    unittest.main()
