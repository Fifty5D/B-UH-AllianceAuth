"""Abstract legacy callback fixtures: annotations are NOT production completion proof.

Raw production-format integration evidence is in test_callback_producer_proof.
"""
import ast
from copy import deepcopy
import hashlib
import time
import unittest

from ops.deploy.docker_host import (
    RetainedSemanticAnalyzer,
    _semantic_log_records,
    prove_semantic_recovery,
)
from tests.deploy.test_semantic_recovery import SOURCES, record, source


SESSION = "f" * 64
OTHER = "e" * 64
PROXY = "nginx/" + "b" * 64
NATIVE = '''
def _check_callback(request):
    try:
        model = CallbackRedirect.objects.get(session_key=request.session.session_key)
        token = Token.objects.get(pk=model.token.pk)
        return token
    except (CallbackRedirect.DoesNotExist, Token.DoesNotExist, AttributeError):
        logger.debug("No callback for %s session %s", request.user, request.session.session_key, exc_info=True)
        return None
'''


def fixture(*, attribute=False, session=SESSION, principal="AnonymousUser"):
    sources = deepcopy(SOURCES)
    sources["esi.decorators"] = source(NATIVE, "esi.decorators")
    tree = ast.parse(NATIVE)
    logged = next(n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                  and ast.unparse(n.func) == "logger.debug")
    lookup = next(n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                  and ast.unparse(n.func) == ("Token.objects.get" if attribute else "CallbackRedirect.objects.get"))
    body = (f"[01/Jan/2026 00:01:00] DEBUG [esi.decorators:{logged}] No callback for {principal} session sha256:{session}\n"
            "Traceback (most recent call last):\n"
            f'  File "/installed/esi/decorators.py", line {lookup}, in _check_callback\n'
            "    token = <redacted>\n")
    body += ("AttributeError: 'NoneType' object has no attribute 'pk'" if attribute else
             '  File "/django/manager.py", line 1, in manager_method\n'
             '    return get()\n  File "/django/query.py", line 1, in get\n'
             '    raise DoesNotExist()\n'
             'esi.models.CallbackRedirect.DoesNotExist: CallbackRedirect matching query does not exist.')
    failed = record(body)
    failed["rows"][0]["session_sha256"] = [session]
    fallback = record(f"[01/Jan/2026 00:01:02] DEBUG [esi.decorators:1] Redirecting {principal} session sha256:{session} to SSO.",
                      at="2026-01-01T00:01:02Z")
    native = record(f"[01/Jan/2026 00:02:00] DEBUG [esi.decorators:1] Got new token from {principal} session sha256:{session}. Returning to view.",
                    at="2026-01-01T00:02:00Z")
    redirect = record('192.0.2.10 - - [01/Jan/2026:00:02:02 +0000] "GET /sso/login HTTP/1.1" 302 0 "-" "synthetic-agent"',
                      at="2026-01-01T00:02:02Z")
    returned = record('192.0.2.10 - - [01/Jan/2026:00:02:03 +0000] "GET /dashboard/ HTTP/1.1" 200 0 "-" "synthetic-agent"',
                      at="2026-01-01T00:02:03Z")
    for row in (redirect, returned):
        row["source"] = PROXY
        row["rows"][0]["session_sha256"] = [session]
    state = {"sources": sources, "inventory_before": [], "owners": [],
             "observed_at": "2026-01-01T00:05:00Z", "identity_preserved": True,
             "credentials_valid": True, "proxy_service": "nginx"}
    return state, [failed, fallback, native, redirect, returned]


def analyze(state, rows):
    with RetainedSemanticAnalyzer(state) as analyzer:
        for row in rows:
            analyzer.feed(deepcopy(row))
        decision = analyzer._callback_decision(rows[0])
        summary = analyzer.finish()
        return decision, summary


def change_session(event, value):
    for row in event["rows"]:
        row["text"] = row["text"].replace(SESSION, value)
        if "session_sha256" in row:
            row["session_sha256"] = [value]


class CallbackCorrelationTests(unittest.TestCase):
    def assert_unproven(self, state, rows):
        decision, summary = analyze(state, rows)
        self.assertEqual(decision, ("handled_callback", "unresolved"))
        self.assertGreater(summary["unresolved_count"] + summary["unknown_count"], 0)

    def test_f02_explicit_other_session_http_return_same_client(self):
        for attribute in (False, True):
            with self.subTest(attribute=attribute):
                state, rows = fixture(attribute=attribute)
                change_session(rows[3], OTHER)
                change_session(rows[4], OTHER)
                self.assert_unproven(state, rows)

    def test_f02_concurrent_other_session_http_return_same_client(self):
        state, rows = fixture(attribute=True)
        other_completion = deepcopy(rows[2])
        change_session(other_completion, OTHER)
        other_completion["first_at"] = "2026-01-01T00:02:01Z"
        rows.insert(3, other_completion)
        change_session(rows[4], OTHER)
        change_session(rows[5], OTHER)
        self.assert_unproven(state, rows)

    def test_same_session_annotations_do_not_prove_actual_operation_completion(self):
        for attribute in (False, True):
            for principal in ("AnonymousUser", "SyntheticUser"):
                with self.subTest(attribute=attribute, principal=principal):
                    state, rows = fixture(attribute=attribute, principal=principal)
                    decision, summary = analyze(state, rows)
                    self.assertEqual(decision, ("handled_callback", "unresolved"))
                    self.assertEqual(summary["unresolved_count"], 1)
                    self.assertEqual(summary["unknown_count"], 0)

    def test_independent_concurrent_session_annotations_are_not_operation_proof(self):
        state, rows = fixture()
        _, other_rows = fixture(session=OTHER)
        decision, summary = analyze(state, rows + other_rows)
        self.assertEqual(decision, ("handled_callback", "unresolved"))
        self.assertEqual(summary["unresolved_count"], 2)

    def test_missing_and_ambiguous_correlation_never_borrows_identity(self):
        for position in range(5):
            for mutation in ("missing", "ambiguous", "contradictory", "malformed"):
                with self.subTest(position=position, mutation=mutation):
                    state, rows = fixture()
                    event = rows[position]
                    if mutation == "missing":
                        event["rows"][0].pop("session_sha256", None)
                        event["rows"][0]["text"] = event["rows"][0]["text"].replace("sha256:" + SESSION, "<redacted>")
                    elif mutation == "ambiguous":
                        event["rows"][0]["session_sha256"] = [SESSION, OTHER]
                    elif mutation == "contradictory":
                        event["rows"][0]["session_sha256"] = [OTHER]
                    else:
                        event["rows"][0]["session_sha256"] = "invalid"
                    self.assert_unproven(state, rows)

    def test_wrong_native_or_http_session_and_missing_events_fail_closed(self):
        for position in range(1, 5):
            for missing in (False, True):
                with self.subTest(position=position, missing=missing):
                    state, rows = fixture()
                    if missing:
                        rows.pop(position)
                    else:
                        change_session(rows[position], OTHER)
                    self.assert_unproven(state, rows)

    def test_success_before_failure_or_outside_continuation_boundaries_is_unproven(self):
        for position, stamp in ((1, "2026-01-01T00:00:59Z"), (1, "2026-01-01T00:01:06Z"),
                                (2, "2026-01-01T00:00:59Z"), (2, "2026-01-01T04:01:01Z"),
                                (3, "2026-01-01T00:00:59Z"), (3, "2026-01-01T00:02:06Z"),
                                (4, "2026-01-01T00:00:59Z"), (4, "2026-01-01T00:02:08Z")):
            with self.subTest(position=position, stamp=stamp):
                state, rows = fixture()
                rows[position]["first_at"] = stamp
                self.assert_unproven(state, rows)

    def test_continuation_cannot_cross_a_new_failure_or_fallback_for_same_session(self):
        for position in (0, 1):
            state, rows = fixture()
            boundary = deepcopy(rows[position])
            boundary["first_at"] = "2026-01-01T00:01:30Z"
            rows.append(boundary)
            with self.subTest(position=position):
                self.assert_unproven(state, rows)

    def test_later_relevant_native_and_http_failures_keep_claim_unproven(self):
        for native in (False, True):
            state, rows = fixture()
            later = deepcopy(rows[0] if native else rows[4])
            later["first_at"] = "2026-01-01T00:04:00Z"
            if not native:
                later["rows"][0]["text"] = later["rows"][0]["text"].replace('200 0', '500 0')
            rows.append(later)
            with self.subTest(native=native):
                self.assert_unproven(state, rows)

    def test_duplicate_physical_records_do_not_create_independent_operations(self):
        state, rows = fixture(attribute=True)
        for event in rows:
            for row in event["rows"]:
                row["at"] = event["first_at"]
                row["raw_sha256"] = hashlib.sha256(row["text"].encode()).hexdigest()
        decision, summary = analyze(state, rows + deepcopy(rows))
        self.assertEqual(decision, ("handled_callback", "unresolved"))
        self.assertEqual(summary["exact_duplicate_physical_views"], 5)
        self.assertEqual(summary["unresolved_count"], 1)

    def test_conflicting_duplicate_views_cannot_hide_another_session(self):
        state, rows = fixture()
        for event in rows:
            for row in event["rows"]:
                row["at"] = event["first_at"]
                row["raw_sha256"] = hashlib.sha256(row["text"].encode()).hexdigest()
        conflict = deepcopy(rows[4])
        conflict["rows"][0]["session_sha256"] = [OTHER]
        rows.append(conflict)
        self.assert_unproven(state, rows)

    def test_distinct_returns_and_completions_for_same_session_are_ambiguous(self):
        for position in (1, 2, 3, 4):
            state, rows = fixture()
            another = deepcopy(rows[position])
            another["first_at"] = {1: "2026-01-01T00:01:03Z", 2: "2026-01-01T00:02:01Z",
                                   3: "2026-01-01T00:02:03Z", 4: "2026-01-01T00:02:04Z"}[position]
            rows.append(another)
            with self.subTest(position=position):
                self.assert_unproven(state, rows)

    def test_same_session_concurrent_request_from_another_client_is_ambiguous(self):
        state, rows = fixture()
        another = deepcopy(rows[4])
        another["rows"][0]["text"] = another["rows"][0]["text"].replace("192.0.2.10", "192.0.2.11")
        another["first_at"] = "2026-01-01T00:02:04Z"
        rows.append(another)
        self.assert_unproven(state, rows)

    def test_same_session_continuation_for_another_principal_or_source_is_ambiguous(self):
        for mutation in ("principal", "source"):
            state, rows = fixture()
            another = deepcopy(rows[2])
            another["first_at"] = "2026-01-01T00:02:01Z"
            if mutation == "principal":
                another["rows"][0]["text"] = another["rows"][0]["text"].replace("AnonymousUser", "AnotherUser")
            else:
                another["source"] = "worker/" + "c" * 64
            rows.append(another)
            with self.subTest(mutation=mutation):
                self.assert_unproven(state, rows)

    def test_recognizing_native_caught_failure_does_not_prove_its_completion(self):
        for mutation in ("attribute", "frame", "severity", "extra_error", "changed_source", "uncaught"):
            state, rows = fixture(attribute=True)
            if mutation == "attribute":
                rows[0]["rows"][-1]["text"] = "AttributeError: 'NoneType' object has no attribute 'anything_else'"
            elif mutation == "frame":
                rows[0]["rows"][2]["text"] = '  File "/unrelated.py", line 1, in _check_callback'
            elif mutation == "severity":
                rows[0]["rows"][0]["text"] = rows[0]["rows"][0]["text"].replace("DEBUG", "ERROR")
            elif mutation == "extra_error":
                rows[0]["rows"].append({"text": "RuntimeError: unknown genuine failure"})
            elif mutation == "changed_source":
                state["sources"]["esi.decorators"]["text"] += "# damaged digest\n"
            else:
                state["sources"]["esi.decorators"] = source(NATIVE.replace(", AttributeError", ""), "esi.decorators")
            decision, summary = analyze(state, rows)
            with self.subTest(mutation=mutation):
                self.assertNotEqual(decision, ("handled_callback", "handled"))
                self.assertGreater(summary["unknown_count"] + summary["unresolved_count"], 0)

    def test_callback_traceback_must_point_to_the_actual_session_lookup(self):
        state, rows = fixture()
        rows[0]["rows"][2]["text"] = '  File "/installed/esi/decorators.py", line 999, in _check_callback'
        self.assert_unproven(state, rows)

    def test_wrong_client_agent_proxy_or_non_success_status_remains_unproven(self):
        for mutation in ("client", "agent", "proxy", "http", "component", "severity", "partial"):
            state, rows = fixture()
            if mutation == "client":
                rows[4]["rows"][0]["text"] = rows[4]["rows"][0]["text"].replace("192.0.2.10", "192.0.2.11")
            elif mutation == "agent":
                rows[4]["rows"][0]["text"] = rows[4]["rows"][0]["text"].replace("synthetic-agent", "another-agent")
            elif mutation == "proxy":
                rows[3]["source"] = rows[4]["source"] = "unrelated/" + "c" * 64
            elif mutation == "http":
                rows[4]["rows"][0]["text"] = rows[4]["rows"][0]["text"].replace('200 0', '500 0')
            elif mutation in ("component", "severity"):
                rows[2]["rows"][0]["text"] = rows[2]["rows"][0]["text"].replace(
                    "esi.decorators" if mutation == "component" else "DEBUG",
                    "unrelated" if mutation == "component" else "ERROR")
            else:
                rows[4]["record_complete"] = False
            with self.subTest(mutation=mutation):
                self.assert_unproven(state, rows)

    def test_arbitrary_view_path_is_not_callback_operation_proof(self):
        state, rows = fixture()
        rows[4]["rows"][0]["text"] = rows[4]["rows"][0]["text"].replace("/dashboard/", "/synthetic-view/?page=1")
        self.assertEqual(analyze(state, rows)[0], ("handled_callback", "unresolved"))

    def test_principal_only_token_selection_and_nearby_dashboard_cannot_clear(self):
        state, rows = fixture(principal="SyntheticUser")
        rows[1:3] = [record("[01/Jan/2026 00:01:03] DEBUG [esi.decorators:1] SyntheticUser has selected token 7",
                            at="2026-01-01T00:01:03Z"),
                     record("[01/Jan/2026 00:01:04] DEBUG [esi.decorators:1] Selected token fulfills requirements of view. Returning.",
                            at="2026-01-01T00:01:04Z")]
        rows.pop(3)
        self.assert_unproven(state, rows)

    def test_direct_contract_annotations_and_prefilled_flags_are_not_causal_proof(self):
        from tests.deploy.test_semantic_recovery import case, operation
        state, rows = fixture()
        item = case("handled_callback")
        item["record"] = rows[0]
        item["success"]["finished_at"] = rows[4]["first_at"]
        for side in ("failure", "success", "current"):
            item[side]["operation"] = operation("application", "esi", "callback_lookup", SESSION)
        item.update(session_sha256=SESSION, success_session_sha256=SESSION,
                    native_fallback_completed=True, authentication_completed=True, http_completion_status=200,
                    proxy_service="nginx", callback_records=dict(zip(
                        ("fallback", "continuation", "redirect", "return"), rows[1:], strict=True)))
        self.assertFalse(prove_semantic_recovery(item, state["sources"]))
        # The analyzer must not construct a success/current observation from
        # these records at all. This catches R1 independently of marker parsing.
        with RetainedSemanticAnalyzer(state) as analyzer:
            for row in rows:
                analyzer.feed(row)
            def forbidden_case(*args, **kwargs):
                raise AssertionError("Unsupported callback evidence fabricated a success")
            analyzer._case = forbidden_case
            self.assertEqual(analyzer._callback_decision(rows[0]), ("handled_callback", "unresolved"))

    def test_live_and_retained_normalization_keep_session_hashes_and_missing_http_proof(self):
        from ops.deploy.docker_host import _semantic_records_from_rows
        state, rows = fixture(attribute=True)
        physical = [{"at": event["first_at"], "text": row["text"]}
                    for event in rows for row in event["rows"]]
        # Plain proxy access records actually have no session identifier. Both
        # adapters must leave this recovery unproven rather than manufacture it.
        live = []
        retained = []
        for source_name in (rows[0]["source"], PROXY):
            selected = physical[:-2] if source_name != PROXY else physical[-2:]
            live.extend(_semantic_log_records((row["at"] + " " + row["text"] for row in selected),
                        source_name, stats={}, deadline=time.monotonic() + 5))
            retained.extend(_semantic_records_from_rows(selected, source_name, stats={}, deadline=time.monotonic() + 5))
        self.assertEqual(live, retained)
        self.assertEqual(live[0]["rows"][0]["session_sha256"], [])
        self.assert_unproven(state, live)
        self.assert_unproven(state, retained)


class CallbackRecordBoundaryTests(unittest.TestCase):
    def test_retained_session_annotations_and_original_hashes_are_idempotent(self):
        from ops.deploy.docker_host import _semantic_records_from_rows
        raw = {"at": "2026-01-01T00:01:00Z", "text":
               "[01/Jan/2026 00:01:00] DEBUG [esi.decorators:1] session synthetic-session-placeholder"}
        raw["text"] = raw["text"].replace("synthetic-session-placeholder", "syntheticsessionkey123456789")
        first = list(_semantic_records_from_rows([raw], "worker/" + "a" * 64, stats={}, deadline=time.monotonic() + 5))
        second = list(_semantic_records_from_rows(first[0]["rows"], first[0]["source"], stats={}, deadline=time.monotonic() + 5))
        self.assertEqual(first, second)
        self.assertNotIn("syntheticsessionkey123456789", str(first))

    def test_truncated_callback_at_eof_or_next_header_retains_partial_and_blocks(self):
        from ops.deploy.docker_host import SemanticRecordError
        state, rows = fixture(attribute=True)
        lines = [rows[0]["first_at"] + " " + row["text"] for row in rows[0]["rows"][:-1]]
        for extra in ([], [rows[1]["first_at"] + " " + rows[1]["rows"][0]["text"]]):
            stats = {}
            with self.subTest(extra=extra), self.assertRaises(SemanticRecordError) as caught:
                list(_semantic_log_records(lines + extra, rows[0]["source"], stats=stats, deadline=time.monotonic() + 5))
            self.assertFalse(stats["read_complete"])
            self.assertFalse(caught.exception.partial_record["record_complete"])
            self.assertTrue(caught.exception.partial_record["rows"])
            with RetainedSemanticAnalyzer(state) as analyzer:
                analyzer.feed(caught.exception.partial_record)
                self.assertFalse(analyzer.finish()["analysis_complete"])

    def test_terminal_and_following_error_remain_in_same_causal_record(self):
        state, rows = fixture(attribute=True)
        lines = [rows[0]["first_at"] + " " + row["text"] for row in rows[0]["rows"]]
        lines += [rows[0]["first_at"] + " RuntimeError: unhandled trailing failure"]
        parsed = list(_semantic_log_records(lines, rows[0]["source"], stats={}, deadline=time.monotonic() + 5))
        self.assertEqual(len(parsed), 1)
        self.assertIn("unhandled trailing failure", parsed[0]["rows"][-1]["text"])
        self.assertEqual(analyze(state, parsed + rows[1:])[0], ("handled_callback", "unresolved"))

    def test_complete_retained_rows_replay_like_their_normalized_equivalent(self):
        from ops.deploy.docker_host import _semantic_records_from_rows
        state, rows = fixture(attribute=True)
        normalized = []
        for source_name in (rows[0]["source"], PROXY):
            physical = [{**row, "at": event["first_at"]} for event in rows
                        if event["source"] == source_name for row in event["rows"]]
            normalized.extend(_semantic_records_from_rows(physical, source_name, stats={}, deadline=time.monotonic() + 5))
        self.assertEqual(analyze(state, rows)[0], ("handled_callback", "unresolved"))
        self.assertEqual(analyze(state, normalized)[0], ("handled_callback", "unresolved"))


if __name__ == "__main__":
    unittest.main()
