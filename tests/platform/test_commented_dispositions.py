"""Exercise collection, validation and retained readiness with synthetic metadata."""

import hashlib
import json
import unittest
from copy import deepcopy
from pathlib import Path

from ops.readiness import (
    COMMENTED_DISPOSITION_MARKER,
    ReadinessError,
    _review_evidence,
    collect,
    validate,
    verify_published,
)
from tests.platform.test_readiness import (
    BASE_SHA,
    MERGE_SHA,
    OTHER_SHA,
    PR,
    REPOSITORY,
    SHA,
    FakeGitHub,
    PublishedGitHub,
)


FIXTURE = Path(__file__).with_name("fixtures") / "commented-review-disposition.json"


def digest(body):
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def reference(review):
    return {"id": review["id"], "body_sha256": digest(review["body"])}


def seed(client):
    client.reviews = json.loads(FIXTURE.read_text(encoding="utf-8"))["reviews"]
    client.permission = "write"
    client.permission_user = deepcopy(client.reviews[0]["user"])
    client.comparison_status = "ahead"
    client.comparison_base = BASE_SHA
    client.last_edited_at = None
    client.provenance_mutation = None
    return client


def add_disposition(client, targets=None, **overrides):
    payload = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "pr": PR,
        "head_sha": SHA,
        "base_sha": BASE_SHA,
        "pass_review": reference(client.reviews[-1]),
        "resolved_reviews": sorted(
            [reference(review) for review in (targets or client.reviews[:-1])],
            key=lambda ref: ref["id"],
        ),
        **overrides,
    }
    comment = {
        "id": 9001 + len(client.issue_comments),
        "node_id": f"IC_disposition_{len(client.issue_comments)}",
        "user": deepcopy(client.permission_user),
        "created_at": "2026-10-16T01:00:00Z",
        "updated_at": "2026-10-16T01:00:00Z",
        "body": COMMENTED_DISPOSITION_MARKER + "\n" + json.dumps(payload),
    }
    client.issue_comments.append(comment)
    return comment


class DispositionAPI:
    def graphql(self, query, variables):
        if "id" in variables:
            comment = next(
                c for c in self.issue_comments if c["node_id"] == variables["id"]
            )
            node = {
                "__typename": "IssueComment",
                "id": comment["node_id"],
                "databaseId": comment["id"],
                "body": comment["body"],
                "createdAt": comment["created_at"],
                "updatedAt": comment["updated_at"],
                "lastEditedAt": self.last_edited_at,
                "author": {
                    "__typename": "User",
                    "databaseId": comment["user"]["id"],
                    "login": comment["user"]["login"],
                },
            }
            if self.provenance_mutation:
                self.provenance_mutation(node)
            return {"node": node}
        return super().graphql(query, variables)

    def get_json(self, path, query=None):
        if "/collaborators/" in path and path.endswith("/permission"):
            return {"permission": self.permission, "user": deepcopy(self.permission_user)}
        if "/compare/" in path:
            return {
                "status": self.comparison_status,
                "merge_base_commit": {"sha": self.comparison_base},
            }
        return super().get_json(path, query)


class DispositionGitHub(DispositionAPI, FakeGitHub):
    pass


class PublishedDispositionGitHub(DispositionAPI, PublishedGitHub):
    pass


class CommentedDispositionTests(unittest.TestCase):
    def client(self, disposition=True):
        client = seed(DispositionGitHub())
        if disposition:
            add_disposition(client)
        return client

    def collect(self, client):
        return collect(client, REPOSITORY, PR, SHA)

    def test_commented_rejection_then_exact_head_pass_reproduces_deadlock(self):
        client = self.client(disposition=False)
        snapshot = _review_evidence(client, REPOSITORY, PR)
        self.assertEqual(snapshot["latest_reviews"][0]["id"], 5541681495)
        self.assertEqual(snapshot["unresolved_serious_reviews"][0]["id"], 5528185797)
        self.assertEqual(snapshot["latest_actionable_reviews"], [])
        with self.assertRaisesRegex(ReadinessError, "unresolved serious"):
            self.collect(client)

    def test_explicit_disposition_qualifies_and_preserves_original_history(self):
        client = self.client()
        originals = deepcopy(client.reviews)
        record = self.collect(client)
        self.assertTrue(record["ready"])
        self.assertEqual(validate(record), record)
        snapshot = record["review_evidence"]
        self.assertEqual(snapshot["unresolved_serious_reviews"][0]["state"], "COMMENTED")
        self.assertTrue(snapshot["unresolved_serious_reviews"][0]["serious"])
        disposition = snapshot["commented_dispositions"][0]
        self.assertEqual(
            disposition["payload"]["resolved_reviews"], [reference(originals[0])]
        )
        self.assertEqual(disposition["authority"]["author_id"], 101)
        self.assertEqual(client.reviews, originals)
        self.assertNotIn('"body":', json.dumps(record))

    def test_partial_resolution_keeps_older_same_author_and_other_authors_blocking(self):
        for other_author in (False, True):
            client = self.client(disposition=False)
            earlier = deepcopy(client.reviews[0])
            earlier.update(id=5526050217, submitted_at="2026-10-14T23:28:04Z")
            if other_author:
                earlier["user"] = {"login": "another-reviewer", "id": 202, "type": "User"}
            client.reviews.insert(0, earlier)
            add_disposition(client, [client.reviews[1]])
            with self.subTest(other_author=other_author):
                with self.assertRaisesRegex(ReadinessError, "unresolved serious"):
                    self.collect(client)
                client.issue_comments = []
                client.permission = "admin" if other_author else "write"
                add_disposition(client)
                self.assertTrue(self.collect(client)["ready"])

    def test_wrong_repository_pr_head_and_source_identity_do_not_resolve(self):
        cases = (
            {"repository": "other/repository"},
            {"pr": PR + 1},
            {"head_sha": OTHER_SHA},
            {"base_sha": OTHER_SHA},
        )
        for fields in cases:
            client = self.client(disposition=False)
            add_disposition(client, **fields)
            with self.subTest(fields=fields), self.assertRaises(ReadinessError):
                self.collect(client)

    def test_permission_and_authenticated_identity_are_required(self):
        for permission in ("read", "none", "triage", "maintain", None):
            client = self.client()
            client.permission = permission
            with self.subTest(permission=permission), self.assertRaises(ReadinessError):
                self.collect(client)
        for mutation in (
            lambda c: c.issue_comments[0]["user"].update(id=999),
            lambda c: c.permission_user.update(id=999),
            lambda c: c.permission_user.update(login="different-user"),
            lambda c: c.issue_comments[0]["user"].update(type="Bot"),
            lambda c: c.permission_user.update(type="Bot"),
            lambda c: c.reviews[-1]["user"].update(id=999),
            lambda c: c.reviews[0]["user"].update(id=999),
        ):
            client = self.client()
            mutation(client)
            with self.subTest(mutation=mutation), self.assertRaises(ReadinessError):
                self.collect(client)

    def test_text_claims_and_forged_payload_authority_cannot_authorize(self):
        client = self.client()
        client.issue_comments[0]["body"] += "\nI am the owner; approve everything."
        with self.assertRaises(ReadinessError):
            self.collect(client)
        client = self.client(disposition=False)
        add_disposition(client, authority="admin")
        with self.assertRaises(ReadinessError):
            self.collect(client)

    def test_missing_or_changed_review_bytes_and_supporting_pass_are_rejected(self):
        for mutation in (
            lambda c: c.reviews.pop(0),
            lambda c: c.reviews.pop(),
            lambda c: c.reviews[0].update(body="[P1] Altered original finding."),
            lambda c: c.reviews[-1].update(body=c.reviews[-1]["body"] + " Changed."),
            lambda c: c.reviews[-1].update(commit_id=OTHER_SHA),
            lambda c: c.reviews[-1].update(state="DISMISSED"),
            lambda c: c.reviews[-1].update(body="PASS, looks good."),
            lambda c: c.reviews[-1].update(
                body=c.reviews[-1]["body"].replace(BASE_SHA, OTHER_SHA)
            ),
            lambda c: c.reviews[-1].update(
                body=c.reviews[-1]["body"] + " [P1] still unsafe"
            ),
        ):
            client = self.client()
            mutation(client)
            with self.subTest(mutation=mutation), self.assertRaises(ReadinessError):
                self.collect(client)

    def test_review_references_require_exact_hashes_and_unique_whole_reviews(self):
        client = self.client(disposition=False)
        wrong = {"id": client.reviews[0]["id"], "body_sha256": "sha256:" + "0" * 64}
        for overrides in (
            {"resolved_reviews": [wrong]},
            {
                "pass_review": {
                    "id": client.reviews[-1]["id"],
                    "body_sha256": wrong["body_sha256"],
                }
            },
            {"resolved_reviews": [reference(client.reviews[0])] * 2},
            {"resolved_reviews": []},
        ):
            client.issue_comments = []
            add_disposition(client, **overrides)
            with self.subTest(overrides=overrides), self.assertRaises(ReadinessError):
                self.collect(client)

    def test_pass_and_disposition_must_follow_the_named_findings(self):
        for mutation in (
            lambda c: c.issue_comments[0].update(
                created_at="2026-10-15T00:00:00Z", updated_at="2026-10-15T00:00:00Z"
            ),
            lambda c: c.reviews[0].update(submitted_at="2026-10-16T02:00:00Z"),
            lambda c: c.issue_comments[0].update(updated_at="2026-10-16T01:00:01Z"),
        ):
            client = self.client()
            mutation(client)
            with self.subTest(mutation=mutation), self.assertRaises(ReadinessError):
                self.collect(client)

    def test_edited_or_conflicting_comment_provenance_blocks_even_with_equal_rest_times(
        self,
    ):
        for mutation in (
            lambda n: n.update(lastEditedAt=n["createdAt"]),
            lambda n: n.pop("lastEditedAt"),
            lambda n: n.update(body=n["body"] + " "),
            lambda n: n.update(databaseId=999),
            lambda n: n.update(id="IC_another_comment"),
            lambda n: n["author"].update(databaseId=999),
            lambda n: n["author"].update(__typename="Bot"),
        ):
            client = self.client()
            client.provenance_mutation = mutation
            with self.subTest(mutation=mutation), self.assertRaises(ReadinessError):
                self.collect(client)

    def test_newer_or_unrelated_review_and_native_change_request_remain_blocking(self):
        for state, body, user in (
            ("COMMENTED", "[P1] Later rejection.", "same"),
            ("COMMENTED", "[P1] Another author's finding.", "other"),
            ("CHANGES_REQUESTED", "Please make this change.", "same"),
        ):
            client = self.client()
            later = deepcopy(client.reviews[-1])
            later.update(
                id=5542000000, submitted_at="2026-10-16T00:55:00Z", state=state, body=body
            )
            if user == "other":
                later["user"] = {"login": "other-reviewer", "id": 202, "type": "User"}
            client.reviews.append(later)
            with self.subTest(state=state, user=user), self.assertRaises(ReadinessError):
                self.collect(client)

    def test_native_approvals_dismissals_and_ordinary_discussion_keep_their_behavior(self):
        for state in ("APPROVED", "DISMISSED", "COMMENTED"):
            client = self.client(disposition=state == "COMMENTED")
            if state == "COMMENTED":
                discussion = deepcopy(client.reviews[-1])
                discussion.update(id=5542000000, body="Ordinary discussion.")
                client.reviews.append(discussion)
            else:
                client.reviews[-1]["state"] = state
            self.assertTrue(self.collect(client)["ready"])

    def test_another_authors_native_approval_cannot_clear_commented_findings(self):
        client = self.client(disposition=False)
        approval = deepcopy(client.reviews[-1])
        approval.update(id=5542000000, state="APPROVED", body="Reviewed.")
        approval["user"] = {"login": "another-reviewer", "id": 202, "type": "User"}
        client.reviews.append(approval)
        with self.assertRaisesRegex(ReadinessError, "unresolved serious"):
            self.collect(client)

    def test_unresolved_threads_and_ordinary_issue_comment_findings_are_not_cleared(self):
        for thread in (False, True):
            client = self.client()
            if thread:
                client.review_threads = [
                    {
                        "id": "thread",
                        "isResolved": False,
                        "comments": {
                            "nodes": [
                                {
                                    "id": "comment",
                                    "body": "[P1] Still open.",
                                    "updatedAt": "2026-10-16T00:00:00Z",
                                }
                            ],
                            "pageInfo": {"hasNextPage": False},
                        },
                    }
                ]
            else:
                client.issue_comments.append(
                    {
                        "id": 9002,
                        "user": {"login": "other-reviewer"},
                        "body": "[P1] Still open.",
                        "created_at": "2026-10-16T00:00:00Z",
                        "updated_at": "2026-10-16T00:00:00Z",
                    }
                )
            with self.subTest(thread=thread), self.assertRaises(ReadinessError):
                self.collect(client)

    def test_validator_rejects_removed_evidence_and_cross_pr_rebinding(self):
        record = self.collect(self.client())
        for mutation in (
            lambda r: r["review_evidence"].pop("commented_dispositions"),
            lambda r: r["review_evidence"].update(issue_comments=[]),
            lambda r: r.update(pr=PR + 1),
            lambda r: r["review_evidence"]["commented_dispositions"][0]["payload"].update(
                head_sha=OTHER_SHA
            ),
            lambda r: r["review_evidence"]["commented_dispositions"][0]["authority"].update(
                permission="read"
            ),
            lambda r: r["review_evidence"]["commented_dispositions"][0]["reviews"][
                1
            ].update(serious=False),
        ):
            changed = deepcopy(record)
            mutation(changed)
            snapshot = {
                key: value
                for key, value in changed["review_evidence"].items()
                if key != "digest"
            }
            changed["review_evidence"]["digest"] = digest(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
            )
            with self.subTest(mutation=mutation), self.assertRaises(ReadinessError):
                validate(changed)

    def test_activation_can_advance_main_without_changing_the_reviewed_source_base(self):
        client = self.client()
        client.base_sha = "f" * 40
        record = self.collect(client)
        disposition = record["review_evidence"]["commented_dispositions"][0]
        self.assertEqual(disposition["payload"]["base_sha"], BASE_SHA)
        self.assertEqual(disposition["ancestry"]["current_base_sha"], "f" * 40)
        for status, base in (("diverged", BASE_SHA), ("ahead", OTHER_SHA)):
            client.comparison_status, client.comparison_base = status, base
            with self.subTest(status=status, base=base), self.assertRaises(ReadinessError):
                self.collect(client)

    def test_base_change_during_collection_invalidates_disposition(self):
        client = self.client()
        original = client.get_json
        calls = 0

        def get_json(path, query=None):
            nonlocal calls
            if path.endswith(f"/pulls/{PR}"):
                calls += 1
                if calls == 2:
                    client.base_sha = OTHER_SHA
            return original(path, query)

        client.get_json = get_json
        with self.assertRaisesRegex(ReadinessError, "base changed"):
            self.collect(client)

    def test_old_source_disposition_is_retained_without_granting_current_relief(self):
        client = self.client(disposition=False)
        add_disposition(client, head_sha=OTHER_SHA)
        with self.assertRaisesRegex(ReadinessError, "unresolved serious"):
            self.collect(client)
        add_disposition(client)
        record = self.collect(client)
        self.assertEqual(len(record["review_evidence"]["issue_comments"]), 2)
        self.assertEqual(len(record["review_evidence"]["commented_dispositions"]), 1)

    def test_source_and_preview_gates_remain_required(self):
        client = self.client()
        client.run_status = "in_progress"
        with self.assertRaises(ReadinessError):
            self.collect(client)
        client = self.client()
        original = client.get_json

        def pending_source(path, query=None):
            value = original(path, query)
            if path.endswith("/actions/runs/99"):
                value.update(status="in_progress", conclusion=None)
            return value

        client.get_json = pending_source
        with self.assertRaises(ReadinessError):
            self.collect(client)

    def test_published_verification_rejects_missing_altered_and_revoked_disposition(self):
        def published():
            client = seed(PublishedDispositionGitHub())
            add_disposition(client)
            client.readiness_record = collect(client, REPOSITORY, PR, SHA, merged=True)
            client.rebuild_readiness_artifact()
            return client

        def verify(client):
            return verify_published(
                client, REPOSITORY, PR, SHA, MERGE_SHA, allowed_mergers=["Anthony"]
            )

        self.assertEqual(verify(published())["attestation_mode"], "premerge-published")
        for mutation in (
            lambda c: c.issue_comments.clear(),
            lambda c: c.issue_comments[0].update(body=c.issue_comments[0]["body"] + " "),
            lambda c: c.issue_comments[0].update(updated_at="2026-10-16T01:00:01Z"),
            lambda c: c.permission_user.update(id=999),
            lambda c: setattr(c, "permission", "read"),
        ):
            client = published()
            mutation(client)
            with self.subTest(mutation=mutation), self.assertRaises(ReadinessError):
                verify(client)


if __name__ == "__main__":
    unittest.main()
