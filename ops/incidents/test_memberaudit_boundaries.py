"""Synthetic selection, accounting, and launcher safety; no production services."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from ops.incidents import memberaudit_selection as select
from ops.incidents import run_memberaudit_recovery as host


def row(pk=1, user=1, section="location", time="2026-10-01T10:40:00+00:00"):
    return {
        "memberaudit_character_pk": pk, "character_id": 900000000 + pk,
        "character_name": "Synthetic " + str(pk), "auth_link_exists": True, "disabled": False,
        "identity": {
            "user_id": user, "ownership_id": pk, "user_active": True,
            "tokens": [{"id": pk, "user_id": user, "character_id": 900000000 + pk,
                        "has_refresh_credential": True, "missing_scopes": [],
                        "owner_identity_matches_auth_link": True}],
        },
        "sections": [{"status_pk": pk, "section": section, "has_token_error": True,
                      "is_success": False, "run_finished_at": time,
                      "error": {"classes": ["TokenDoesNotExist"], "oauth_codes": [],
                                "message_sha256": hashlib.sha256(b"synthetic").hexdigest()}}],
    }


class MemberAuditBoundaryTests(unittest.TestCase):
    def report(self, rows):
        return {"database": {"memberaudit": rows}}

    def test_freezes_only_october_error_rows_not_older_or_successful_sections(self):
        old = row(2, time="2026-09-01T10:00:00+00:00")
        old["disabled"], old["auth_link_exists"], old["identity"] = True, False, None
        current = row()
        other = copy.deepcopy(current["sections"][0])
        other.update(status_pk=1000, has_token_error=False, is_success=True)
        current["sections"].append(other)
        targets, excluded = select.select_outage(self.report([current, old]))
        self.assertEqual(len(targets), 1)
        self.assertEqual(len(targets[0]["sections"]), 1)
        self.assertEqual(excluded, [{"status_pk": 2, "memberaudit_character_pk": 2}])

    def test_real_permanent_error_shape_cannot_be_swept_into_outage_selection(self):
        current = row()
        current["sections"][0]["error"]["classes"] = ["InvalidGrantError"]
        with self.assertRaises(select.SelectionStop):
            select.select_outage(self.report([current]))

    def test_mixed_older_and_outage_failure_does_not_expand_selection(self):
        current = row()
        current["sections"].append(row(2, time="2026-09-01T10:00:00+00:00")["sections"][0])
        with self.assertRaises(select.SelectionStop):
            select.select_outage(self.report([current]))

    def test_foreign_user_missing_scopes_or_unsigned_identity_are_not_eligible(self):
        for field, value in (("user_id", 999), ("missing_scopes", ["synthetic-missing"]),
                             ("owner_identity_matches_auth_link", False)):
            with self.subTest(field=field):
                current = row()
                current["identity"]["tokens"][0][field] = value
                with self.assertRaises(select.SelectionStop):
                    select.select_outage(self.report([current]))

    def test_pilot_four_sections_and_representatives_cover_distinct_users(self):
        current = row()
        current["sections"] = []
        for i, section in enumerate(("location", "online_status", "ship", "skill_queue"), 1):
            item = row(i, section=section)["sections"][0]
            current["sections"].append(item)
        targets, _ = select.select_outage(self.report([current, row(10, 2), row(11, 3), row(12, 4)]))
        order = select.pilot_order(targets, "Synthetic 1")
        self.assertEqual(order[0]["memberaudit_character_pk"], 1)
        self.assertEqual(len({t["user_id"] for t in order[:4]}), 4)

    def test_partial_commits_and_unattempted_are_not_counted_as_full_character_recovery(self):
        target = {"memberaudit_character_pk": 1, "user_id": 4,
                  "sections": [{"section": "location"}, {"section": "ship"}]}
        remaining = {"memberaudit_character_pk": 2, "user_id": 5, "sections": [{"section": "mails"}]}
        counts = select.summarize([target, remaining], [{
            "memberaudit_character_pk": 1, "category": "transient_failure",
            "recovered": False, "sections": [{"verified": True}], "requires_reauthorization": False,
        }])
        self.assertEqual(counts["recovered_with_existing_tokens"], 0)
        self.assertEqual(counts["sections_successfully_recovered"], 1)
        self.assertEqual(counts["not_attempted"], 1)
        self.assertEqual(counts["still_retryable_transient"], 1)
        self.assertEqual(counts["users_requiring_reauthorization"], 0)

    def test_users_requiring_reauthorization_are_unique_and_only_proven(self):
        targets = [{"memberaudit_character_pk": i, "user_id": 8, "sections": [{}]} for i in (1, 2)]
        results = [{"memberaudit_character_pk": i, "category": "permanent_invalid_grant",
                    "recovered": False, "requires_reauthorization": True, "sections": []} for i in (1, 2)]
        counts = select.summarize(targets, results)
        self.assertEqual(counts["permanently_invalid_revoked"], 2)
        self.assertEqual(counts["users_requiring_reauthorization"], 1)

    def test_ambiguous_or_missing_json_evidence_fails_closed(self):
        with self.assertRaises(host.RecoveryGate):
            host.parse_result("synthetic private console output")
        with self.assertRaises(host.RecoveryGate):
            host.parse_result("BUH_MEMBERAUDIT_BEGIN\n{}\nBUH_MEMBERAUDIT_END")

    def test_host_routes_hold_and_live_services_cannot_change(self):
        state = {k: "synthetic" for k in (
            "hold_sha256", "active_upstream_sha256", "live_auth_services",
            "runtime_container_id", "active_upstream_container_ids")}
        state["disk"] = {"free_bytes": 10 * 1024 ** 3}
        for key in ("hold_sha256", "active_upstream_sha256", "live_auth_services"):
            after = copy.deepcopy(state)
            after[key] = "changed"
            with self.assertRaises(host.RecoveryGate):
                host.same_host(state, after)
        after = copy.deepcopy(state)
        after["disk"]["free_bytes"] -= 513 * 1024 ** 2
        with self.assertRaises(host.RecoveryGate):
            host.same_host(state, after)

    def test_memberaudit_host_never_invokes_retained_deployment_recovery(self):
        from pathlib import Path
        source = Path(host.__file__).read_text()
        for prohibited in ("recover_incomplete_plan(", "remove_safety_containers(", '["docker", "rm"', '["docker", "stop"'):
            self.assertNotIn(prohibited, source)


class MemberAuditSequenceTests(unittest.TestCase):
    def invoke(self, failure_at=None):
        from ops.deploy.contracts import ReceiverConfig
        targets = []
        for i in range(1, 88):
            targets.append({
                "memberaudit_character_pk": i, "character_id": 900000000 + i,
                "character_name": "Synthetic " + str(i), "user_id": i,
                "auth_link_pk": i, "token_ids": [i],
                "sections": [{"section": name} for name in (
                    "location", "online_status", "ship", "skill_queue")],
            })
        excluded = [{"status_pk": i + 1000, "memberaudit_character_pk": 1000 + i % 12}
                    for i in range(61)]
        snapshot = {"sections": 61, "characters": 12, "states_sha256": "e" * 64}
        state = {
            "hold_sha256": host.HOLD_SHA, "active_upstream_sha256": "b" * 64,
            "live_auth_services": {"web": [{"container_id": "c" * 64}]},
            "runtime_container_id": "c" * 64, "active_upstream_container_ids": ["c" * 64],
            "platform_version": "0.8.2", "disk": {"free_bytes": 100 * 1024 ** 3},
        }
        pilot = MagicMock()
        pilot.latest_report.return_value = (Path("/root/synthetic-baseline"), {"attempt_id": "gh-111-1"}, host.BASELINE_SHA)
        pilot.verify_host.return_value = state
        config = SimpleNamespace(state_dir=Path("/root/synthetic-state"),
                                 gunicorn_service="web", manage_py="manage.py")
        engine = MagicMock()
        count = 0

        def run_shell(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                value = {"schema_version": 1, "recovered": False,
                         "category": "eligible_existing_token_not_yet_validated",
                         "excluded_snapshot": snapshot}
            else:
                index = count - 1
                success = index != failure_at
                value = {
                    "schema_version": 1, "memberaudit_character_pk": index,
                    "recovered": success, "category": "recovered_existing_token" if success else "provider_throttling",
                    "requires_reauthorization": False, "excluded_snapshot": snapshot,
                    "sections": [{"verified": True} for _ in range(4)] if success else [],
                }
            return "BUH_MEMBERAUDIT_BEGIN\n" + json.dumps(value) + "\nBUH_MEMBERAUDIT_END"

        engine._compose.side_effect = run_shell
        with (patch.object(host, "qualified_host", return_value=pilot),
              patch.object(host, "select_outage", return_value=(targets, excluded)),
              patch.object(host, "checkpoint") as saved,
              patch.object(host, "retained_fingerprint", return_value={"synthetic": "preserved"}),
              patch.object(ReceiverConfig, "load", return_value=config),
              patch("ops.deploy.docker_host.DockerHost", return_value=engine),
              patch.object(host.os, "geteuid", return_value=0),
              patch.object(host.os, "open", return_value=123),
              patch.object(host.os, "close"),
              patch.object(host.fcntl, "flock")):
            result = host.recover("gh-111-1", "Synthetic 1", "apply", "/root/synthetic-stage")
        self.assertGreater(saved.call_count, 1)
        # No retained-plan method may have been called.
        self.assertEqual([call[0] for call in engine.method_calls], ["_compose"] * count)
        return result, count

    def test_pilot_failure_stops_before_representatives_or_bulk(self):
        result, count = self.invoke(failure_at=1)
        self.assertTrue(result["scan_complete"], result)
        self.assertFalse(result["recovery_complete"])
        self.assertEqual(count, 2)
        self.assertEqual(result["counts"]["not_attempted"], 86)
        self.assertEqual(result["stop_reason"], "provider_throttling")
        self.assertTrue(result["retained_resources_unchanged_verified"])

    def test_representative_failure_stops_before_later_batches(self):
        result, count = self.invoke(failure_at=3)
        self.assertEqual(count, 4)
        self.assertEqual(result["counts"]["recovered_with_existing_tokens"], 2)
        self.assertEqual(result["counts"]["sections_successfully_recovered"], 8)
        self.assertEqual(result["counts"]["not_attempted"], 84)

    def test_all_success_finishes_exact_roster_and_counts_then_stops(self):
        result, count = self.invoke()
        self.assertTrue(result["recovery_complete"], result)
        self.assertEqual(count, 88)
        self.assertEqual(result["counts"]["recovered_with_existing_tokens"], 87)
        self.assertEqual(result["counts"]["sections_successfully_recovered"], 348)
        self.assertTrue(result["retained_resources_unchanged_verified"])
        self.assertEqual([b["size"] for b in result["batches"]][:3], [1, 3, 5])
        self.assertLessEqual(max(b["size"] for b in result["batches"]), 10)


if __name__ == "__main__":
    unittest.main()
