"""Synthetic selection, accounting, and launcher safety; no production services."""
import copy
import hashlib
import unittest

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


if __name__ == "__main__":
    unittest.main()
