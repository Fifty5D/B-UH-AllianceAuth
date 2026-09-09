from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "ops" / "release"))

import open_sync_pr as sync  # noqa: E402
import platform_approval as approval  # noqa: E402


OWNER = "Fifty5D"
REPOSITORY = "Fifty5D/B-UH-AllianceAuth"
VERSION = "0.4.9"
SOURCE = "1" * 40
RELEASE = "2" * 40
MERGE = "3" * 40
MANIFEST = "4" * 64
FIRST_PARENT = SOURCE
FEATURE_HEAD = "6" * 40
PR_NUMBER = 31
FEATURE_PR = 44
PREFLIGHT_RUN = 555
SOURCE_RUN = 444
MAIN_RUN = 333
FEATURE_CHECK_RUN = 222
FEATURE_VALIDATION_RUN = 111
ARTIFACT = f"platform-v2-preflight-{PREFLIGHT_RUN}-1"
ARTIFACT_ID = 990
ARTIFACT_DIGEST = "sha256:" + "7" * 64
REVIEW_DIGEST = "sha256:" + "8" * 64
READINESS_DIGEST = "sha256:" + "9" * 64
TOKEN = "test-token-that-must-not-be-rendered"


def _response(value: Any) -> sync.HttpResponse:
    return sync.HttpResponse(
        200, (json.dumps(value, separators=(",", ":")) + "\n").encode()
    )


def _run(*, kind: str) -> dict[str, Any]:
    if kind == "preflight":
        return {
            "conclusion": "success",
            "event": "workflow_run",
            "head_branch": "main",
            "head_repository": {"full_name": REPOSITORY},
            "head_sha": SOURCE,
            "id": PREFLIGHT_RUN,
            "path": ".github/workflows/auto-platform-release.yml@refs/heads/main",
            "repository": {"full_name": REPOSITORY},
            "run_attempt": 1,
            "status": "completed",
        }
    if kind == "sync":
        return {
            "conclusion": "success",
            "event": "pull_request",
            "head_branch": f"sync/platform-v{VERSION}",
            "head_repository": {"full_name": REPOSITORY},
            "head_sha": RELEASE,
            "id": SOURCE_RUN,
            "path": ".github/workflows/source-ci.yml@refs/pull/31/merge",
            "pull_requests": [{"number": PR_NUMBER}],
            "run_attempt": 1,
            "status": "completed",
            "repository": {"full_name": REPOSITORY},
        }
    if kind == "main":
        return {
            "conclusion": "success",
            "event": "push",
            "head_branch": "main",
            "head_repository": {"full_name": REPOSITORY},
            "head_sha": SOURCE,
            "id": MAIN_RUN,
            "path": ".github/workflows/source-ci.yml@refs/heads/main",
            "repository": {"full_name": REPOSITORY},
            "run_attempt": 1,
            "status": "completed",
        }
    raise AssertionError(f"unknown run kind: {kind}")


def _published_readiness() -> dict[str, Any]:
    readiness_name = (
        f"pr-readiness-{FEATURE_PR}-{FEATURE_HEAD}-"
        f"{REVIEW_DIGEST.removeprefix('sha256:')}"
    )
    return {
        "attestation_mode": "premerge-published",
        "feature_pr": {"head_sha": FEATURE_HEAD, "number": FEATURE_PR},
        "feature_validation": {
            "check_run_id": FEATURE_CHECK_RUN,
            "workflow_run_attempt": 1,
            "workflow_run_id": FEATURE_VALIDATION_RUN,
        },
        "merge_source_commit": SOURCE,
        "merger": OWNER,
        "preview": None,
        "preview_required": False,
        "readiness": {
            "artifact_digest": READINESS_DIGEST,
            "artifact_id": 777,
            "artifact_name": readiness_name,
            "review_digest": REVIEW_DIGEST,
            "workflow_run_attempt": 1,
            "workflow_run_id": 888,
        },
        "repository": REPOSITORY,
        "review_digest": REVIEW_DIGEST,
        "schema_version": 1,
    }


def _ready_payload() -> dict[str, Any]:
    payload = {
        "feature_readiness": _published_readiness(),
        "main_validation_run_attempt": 1,
        "main_validation_run_id": MAIN_RUN,
        "manifest_sha256": MANIFEST,
        "platform_version": VERSION,
        "preflight_artifact": ARTIFACT,
        "preflight_artifact_digest": ARTIFACT_DIGEST,
        "preflight_artifact_id": ARTIFACT_ID,
        "preflight_run_attempt": 1,
        "preflight_run_id": PREFLIGHT_RUN,
        "pull_request": PR_NUMBER,
        "release_commit": RELEASE,
        "repository": REPOSITORY,
        "schema_version": 2,
        "source_commit": SOURCE,
        "sync_validation_run_attempt": 1,
        "sync_validation_run_id": SOURCE_RUN,
    }
    payload["approval_nonce"] = approval._nonce(payload)
    return payload


def _ready_main_arguments(root: Path) -> list[str]:
    feature_readiness = root / "feature-readiness.json"
    feature_readiness.write_text(
        json.dumps(_published_readiness(), sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="ascii",
    )
    return [
        "ready",
        "--owner",
        OWNER,
        "--repository",
        REPOSITORY,
        "--api-url",
        "https://api.github.com",
        "--server-url",
        "https://github.com",
        "--output",
        str(root / "approval.json"),
        "--version",
        VERSION,
        "--source-commit",
        SOURCE,
        "--release-commit",
        RELEASE,
        "--pull-request",
        str(PR_NUMBER),
        "--feature-readiness",
        str(feature_readiness),
        "--main-validation-run-id",
        str(MAIN_RUN),
        "--main-validation-run-attempt",
        "1",
        "--manifest-sha256",
        MANIFEST,
        "--preflight-run-id",
        str(PREFLIGHT_RUN),
        "--preflight-run-attempt",
        "1",
        "--preflight-artifact",
        ARTIFACT,
    ]


def _event() -> dict[str, Any]:
    return {
        "action": "closed",
        "number": PR_NUMBER,
        "sender": {"login": OWNER},
        "pull_request": {
            "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
            "draft": False,
            "head": {
                "ref": f"sync/platform-v{VERSION}",
                "repo": {"full_name": REPOSITORY},
                "sha": RELEASE,
            },
            "html_url": f"https://github.com/{REPOSITORY}/pull/{PR_NUMBER}",
            "merge_commit_sha": MERGE,
            "merged": True,
            "merged_at": "2026-09-04T12:02:00Z",
            "merged_by": {"login": OWNER},
            "number": PR_NUMBER,
            "state": "closed",
            "title": f"Sync platform release v{VERSION}",
            "user": {"login": OWNER},
        },
    }


class ApprovalTransport:
    def __init__(self, *, include_merge_marker: bool = True) -> None:
        self.include_merge_marker = include_merge_marker
        self.calls: list[tuple[str, str]] = []
        self.source_run = _run(kind="sync")
        self.source_runs = [self.source_run]
        self.main_run = _run(kind="main")
        self.associated_pulls = [_event()["pull_request"]]
        self.artifact_id = ARTIFACT_ID
        self.artifact_digest = ARTIFACT_DIGEST
        self.artifact_expired = False
        self.ready_comment_present = True
        self.main_tip = MERGE
        self.merge_first_parent = FIRST_PARENT
        self.artifact_workflow_run = {
            "head_branch": "main",
            "head_sha": SOURCE,
            "id": PREFLIGHT_RUN,
        }

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: int,
    ) -> sync.HttpResponse:
        del headers, body, timeout
        parsed = urllib.parse.urlsplit(url)
        self.calls.append((method, parsed.path))
        self.assert_get(method)
        suffix = parsed.path.removeprefix(f"/repos/{REPOSITORY}/")
        if suffix == f"commits/{RELEASE}":
            return _response({"sha": RELEASE, "parents": [{"sha": SOURCE}]})
        if suffix == f"git/ref/heads/release/platform-v{VERSION}":
            return _response(
                {
                    "ref": f"refs/heads/release/platform-v{VERSION}",
                    "object": {"sha": RELEASE, "type": "commit"},
                }
            )
        if suffix == f"commits/{MERGE}":
            message = f"Sync platform release v{VERSION}"
            if self.include_merge_marker:
                message += (
                    "\n\nApproved in ChatGPT for production.\n\n"
                    + approval.marker(
                        approval.APPROVAL_PREFIX,
                        approval.approval_payload(_ready_payload()),
                    )
                )
            return _response(
                {
                    "commit": {"message": message},
                    "sha": MERGE,
                    "parents": [{"sha": self.merge_first_parent}, {"sha": RELEASE}],
                }
            )
        if suffix == "git/ref/heads/main":
            return _response(
                {
                    "ref": "refs/heads/main",
                    "object": {"sha": self.main_tip, "type": "commit"},
                }
            )
        if suffix == f"issues/{PR_NUMBER}/comments":
            ready = _ready_payload()
            if not self.ready_comment_present:
                return _response([])
            return _response(
                [
                {
                    "body": approval.marker(approval.READY_PREFIX, ready),
                    "created_at": "2026-09-04T12:00:00Z",
                    "id": 700,
                    "user": {"login": approval.BOT_LOGIN},
                }
                ]
            )
        if suffix == f"actions/runs/{PREFLIGHT_RUN}":
            return _response(_run(kind="preflight"))
        if suffix == f"actions/runs/{MAIN_RUN}":
            return _response(self.main_run)
        if suffix == f"actions/runs/{SOURCE_RUN}":
            return _response(self.source_run)
        if suffix == "actions/workflows/source-ci.yml/runs":
            return _response(
                {
                    "total_count": len(self.source_runs),
                    "workflow_runs": self.source_runs,
                }
            )
        if suffix == f"commits/{RELEASE}/pulls":
            return _response(self.associated_pulls)
        if suffix == f"actions/runs/{PREFLIGHT_RUN}/artifacts":
            return _response(
                {
                    "total_count": 1,
                    "artifacts": [
                        {
                            "expired": self.artifact_expired,
                            "id": self.artifact_id,
                            "name": ARTIFACT,
                            "digest": self.artifact_digest,
                            "size_in_bytes": 4096,
                            "workflow_run": self.artifact_workflow_run,
                        }
                    ]
                }
            )
        raise AssertionError(f"unexpected request: {method} {url}")

    @staticmethod
    def assert_get(method: str) -> None:
        if method != "GET":
            raise AssertionError(f"unexpected method: {method}")


class ReadyTransport(ApprovalTransport):
    def __init__(self) -> None:
        super().__init__()
        self.main_tip = SOURCE

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: int,
    ) -> sync.HttpResponse:
        del headers, timeout
        parsed = urllib.parse.urlsplit(url)
        suffix = parsed.path.removeprefix(f"/repos/{REPOSITORY}/")
        if suffix == f"pulls/{PR_NUMBER}" and method == "GET":
            self.calls.append((method, parsed.path))
            pr = copy.deepcopy(_event()["pull_request"])
            pr["state"] = "open"
            return _response(pr)
        if suffix == f"git/ref/heads/sync/platform-v{VERSION}" and method == "GET":
            self.calls.append((method, parsed.path))
            return _response(
                {
                    "ref": f"refs/heads/sync/platform-v{VERSION}",
                    "object": {"sha": RELEASE, "type": "commit"},
                }
            )
        if suffix == f"actions/runs/{PREFLIGHT_RUN}" and method == "GET":
            self.calls.append((method, parsed.path))
            run = _run(kind="preflight")
            run["status"] = "in_progress"
            run["conclusion"] = None
            return _response(run)
        if suffix == "actions/workflows/source-ci.yml/runs" and method == "GET":
            self.calls.append((method, parsed.path))
            return _response(
                {
                    "total_count": len(self.source_runs),
                    "workflow_runs": self.source_runs,
                }
            )
        if suffix == f"issues/{PR_NUMBER}/comments":
            self.calls.append((method, parsed.path))
            if method == "GET":
                return _response([])
            if method == "POST" and body is not None:
                posted = json.loads(body)
                return sync.HttpResponse(
                    201,
                    (
                        json.dumps(
                            {
                                "body": posted["body"],
                                "id": 701,
                                "user": {"login": approval.BOT_LOGIN},
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode(),
                )
        return super().request(method, url, {}, body, 0)


class DeniedReadyTransport(ReadyTransport):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: int,
    ) -> sync.HttpResponse:
        parsed = urllib.parse.urlsplit(url)
        suffix = parsed.path.removeprefix(f"/repos/{REPOSITORY}/")
        if suffix == f"issues/{PR_NUMBER}/comments" and method == "POST":
            del headers, body, timeout
            self.calls.append((method, parsed.path))
            return sync.HttpResponse(403, b'{"message":"private response body"}\n')
        return super().request(method, url, headers, body, timeout)


class PlatformApprovalTests(unittest.TestCase):
    def authorize_with(self, transport):
        with tempfile.TemporaryDirectory() as temp:
            return approval.authorize(
                _event(), owner=OWNER, repository=REPOSITORY,
                actor=OWNER, triggering_actor=OWNER, run_attempt=1,
                api_url="https://api.github.com", server_url="https://github.com",
                output=Path(temp) / "approval.json", token=TOKEN,
                transport=transport, sleeper=lambda _: None,
            )

    def test_authorize_accepts_empty_run_pr_list_after_merge(self):
        # Real v0.5.2 failure: GitHub removed this association after PR #32
        # merged, while the exact successful Source CI run remained intact.
        transport = ApprovalTransport()
        transport.source_run["pull_requests"] = []
        report = self.authorize_with(transport)
        self.assertEqual(report["release_commit"], RELEASE)
        self.assertIn(
            ("GET", f"/repos/{REPOSITORY}/commits/{RELEASE}/pulls"),
            transport.calls,
        )

    def test_authorize_with_populated_pr_list_needs_no_fallback(self):
        transport = ApprovalTransport()
        self.authorize_with(transport)
        self.assertFalse(any(path.endswith("/pulls") for _, path in transport.calls))

    def test_empty_pr_list_cannot_replace_exact_source_run_evidence(self):
        changes = {
            "id": SOURCE_RUN + 1, "run_attempt": 2,
            "status": "in_progress", "conclusion": "failure", "event": "push",
            "head_sha": SOURCE, "head_branch": "main",
            "path": ".github/workflows/other.yml",
            "head_repository": {"full_name": "another/repository"},
            "repository": {"full_name": "another/repository"},
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                transport = ApprovalTransport()
                transport.source_run.update({"pull_requests": [], field: value})
                with self.assertRaisesRegex(approval.ApprovalError, "Source CI evidence"):
                    self.authorize_with(transport)
                self.assertFalse(any(path.endswith("/pulls") for _, path in transport.calls))

    def test_fallback_rejects_missing_or_conflicting_pr_list(self):
        for pulls in (None, {}, "", [{"number": PR_NUMBER + 1}], [None]):
            with self.subTest(pulls=pulls):
                transport = ApprovalTransport()
                transport.source_run["pull_requests"] = pulls
                with self.assertRaises(approval.ApprovalError):
                    self.authorize_with(transport)
                self.assertFalse(any(path.endswith("/pulls") for _, path in transport.calls))
        transport = ApprovalTransport()
        del transport.source_run["pull_requests"]
        with self.assertRaises(approval.ApprovalError):
            self.authorize_with(transport)

    def test_fallback_rejects_absent_ambiguous_or_unmerged_association(self):
        pr = _event()["pull_request"]
        for pulls in ([], [pr, pr], [{**pr, "number": PR_NUMBER + 1}],
                      [{**pr, "merged_at": None}], [{**pr, "merged_at": "invalid"}]):
            with self.subTest(pulls=pulls):
                transport = ApprovalTransport()
                transport.source_run["pull_requests"] = []
                transport.associated_pulls = pulls
                with self.assertRaises(approval.ApprovalError):
                    self.authorize_with(transport)

    def test_fallback_revalidates_the_exact_merged_pr_identity(self):
        changes = [
            ("state", "open"), ("draft", True), ("title", "A different change"),
            ("html_url", "https://github.com/another/repo/pull/31"),
            ("user", {"login": "another-user"}),
            ("head.ref", "different-branch"), ("head.sha", SOURCE),
            ("head.repo", {"full_name": "another/repository"}),
            ("base.ref", "different-base"),
            ("base.repo", {"full_name": "another/repository"}),
        ]
        for field, value in changes:
            with self.subTest(field=field):
                transport = ApprovalTransport()
                transport.source_run["pull_requests"] = []
                pr = copy.deepcopy(_event()["pull_request"])
                if "." in field:
                    parent, child = field.split(".")
                    pr[parent][child] = value
                else:
                    pr[field] = value
                transport.associated_pulls = [pr]
                with self.assertRaises(approval.ApprovalError):
                    self.authorize_with(transport)

    def test_empty_pr_fallback_still_requires_chatgpt_approval(self):
        transport = ApprovalTransport(include_merge_marker=False)
        transport.source_run["pull_requests"] = []
        with self.assertRaisesRegex(approval.ApprovalError, "lacks the ChatGPT approval"):
            self.authorize_with(transport)
        self.assertFalse(any(path.endswith("/pulls") for _, path in transport.calls))

    def test_readiness_still_requires_direct_pr_run_association(self):
        transport = ApprovalTransport()
        transport.source_run["pull_requests"] = []
        class Client:
            def get(self, path, query):
                return {"workflow_runs": [transport.source_run]}
        with tempfile.TemporaryDirectory() as temp:
            config = approval._config(
                owner=OWNER, repository=REPOSITORY, version=VERSION,
                source_commit=SOURCE, release_commit=RELEASE,
                api_url="https://api.github.com", server_url="https://github.com",
                output=Path(temp) / "ready.json",
            )
            with self.assertRaisesRegex(approval.ApprovalError, "timeout"):
                approval._source_ci_run(
                    Client(), config, PR_NUMBER, sleeper=lambda _: None, polls=1,
                )

    def test_marker_is_canonical_and_approval_binds_the_release(self) -> None:
        ready = _ready_payload()
        encoded = approval.marker(approval.READY_PREFIX, ready)
        self.assertEqual(
            approval._marker_payload(encoded, approval.READY_PREFIX), ready
        )
        approved = approval.approval_payload(ready)
        self.assertEqual(approved["approval_nonce"], ready["approval_nonce"])
        self.assertEqual(approved["release_commit"], RELEASE)
        noncanonical = encoded.replace('"approval_nonce":', '"approval_nonce": ')
        with self.assertRaisesRegex(approval.ApprovalError, "not canonical"):
            approval._marker_payload(noncanonical, approval.READY_PREFIX)

    def test_exact_published_readiness_shape_is_accepted_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = approval._config(
                owner=OWNER,
                repository=REPOSITORY,
                version=VERSION,
                source_commit=SOURCE,
                release_commit=RELEASE,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path(temp) / "ready.json",
            )
            published = _published_readiness()
            canonical = json.dumps(published, sort_keys=True, separators=(",", ":"))
            self.assertEqual(
                approval._feature_readiness(json.loads(canonical), config), published
            )
            mismatched = copy.deepcopy(published)
            mismatched["readiness"]["review_digest"] = "sha256:" + "a" * 64
            with self.assertRaisesRegex(approval.ApprovalError, "does not match"):
                approval._feature_readiness(mismatched, config)
            bootstrap = copy.deepcopy(published)
            bootstrap["attestation_mode"] = "first-introduction-postmerge"
            bootstrap["readiness"] = None
            self.assertEqual(
                approval._feature_readiness(bootstrap, config), bootstrap
            )
            wrong_bootstrap = copy.deepcopy(bootstrap)
            wrong_bootstrap["feature_pr"]["number"] = FEATURE_PR + 1
            with self.assertRaisesRegex(approval.ApprovalError, "pull request 44"):
                approval._feature_readiness(wrong_bootstrap, config)
            previewed = copy.deepcopy(published)
            previewed["preview_required"] = True
            previewed["preview"] = {
                "run_id": 901,
                "run_attempt": 1,
                "manifest_artifact_id": 902,
                "manifest_artifact_digest": "sha256:" + "b" * 64,
                "evidence_artifact_id": 903,
                "evidence_artifact_digest": "sha256:" + "c" * 64,
            }
            self.assertEqual(
                approval._feature_readiness(previewed, config), previewed
            )
            previewed["preview"]["unexpected"] = True
            with self.assertRaisesRegex(approval.ApprovalError, "preview evidence schema"):
                approval._feature_readiness(previewed, config)

    def test_nonce_binds_complete_feature_readiness(self) -> None:
        original = _ready_payload()
        changed = copy.deepcopy(original)
        changed["feature_readiness"]["merger"] = "chatgpt-work"
        self.assertNotEqual(approval._nonce(original), approval._nonce(changed))

    def test_authorize_accepts_one_exact_chatgpt_approved_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            transport = ApprovalTransport()
            report = approval.authorize(
                _event(),
                owner=OWNER,
                repository=REPOSITORY,
                actor=OWNER,
                triggering_actor=OWNER,
                run_attempt=1,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path(temp) / "approval.json",
                token=TOKEN,
                transport=transport,
                sleeper=lambda _: None,
            )
        self.assertEqual(report["release_commit"], RELEASE)
        self.assertEqual(report["source_commit"], SOURCE)
        self.assertEqual(report["preflight_artifact_id"], 990)
        self.assertEqual(report["preflight_artifact_digest"], ARTIFACT_DIGEST)
        self.assertEqual(report["feature_pr_number"], FEATURE_PR)
        self.assertEqual(report["feature_head_sha"], FEATURE_HEAD)
        self.assertEqual(report["approval_record"], "merge-commit")
        self.assertFalse(any(TOKEN in url for _, url in transport.calls))

    def test_authorize_fails_without_chatgpt_approval_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(
            approval.ApprovalError, "lacks the ChatGPT approval marker"
        ):
            approval.authorize(
                _event(),
                owner=OWNER,
                repository=REPOSITORY,
                actor=OWNER,
                triggering_actor=OWNER,
                run_attempt=1,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path(temp) / "approval.json",
                token=TOKEN,
                transport=ApprovalTransport(include_merge_marker=False),
                sleeper=lambda _: None,
            )

    def test_authorize_rejects_a_sync_merge_based_on_stale_main(self) -> None:
        transport = ApprovalTransport()
        transport.merge_first_parent = "5" * 40
        with self.assertRaisesRegex(approval.ApprovalError, "tested source"):
            self.authorize_with(transport)

    def test_authorize_rejects_approval_queued_behind_a_new_main_tip(self) -> None:
        transport = ApprovalTransport()
        transport.main_tip = "5" * 40
        with self.assertRaisesRegex(approval.ApprovalError, "current main tip"):
            self.authorize_with(transport)

    def test_authorize_rechecks_main_after_all_other_live_evidence(self) -> None:
        class AdvancingMainTransport(ApprovalTransport):
            def __init__(self) -> None:
                super().__init__()
                self.main_reads = 0

            def request(self, method, url, headers, body, timeout):
                if urllib.parse.urlsplit(url).path.endswith("/git/ref/heads/main"):
                    self.main_reads += 1
                    if self.main_reads > 1:
                        self.main_tip = "5" * 40
                return super().request(method, url, headers, body, timeout)

        with self.assertRaisesRegex(approval.ApprovalError, "current main tip"):
            self.authorize_with(AdvancingMainTransport())

    def test_authorize_rechecks_artifact_after_all_other_live_evidence(self) -> None:
        class ExpiringArtifactTransport(ApprovalTransport):
            def __init__(self) -> None:
                super().__init__()
                self.artifact_reads = 0

            def request(self, method, url, headers, body, timeout):
                if urllib.parse.urlsplit(url).path.endswith(
                    f"/actions/runs/{PREFLIGHT_RUN}/artifacts"
                ):
                    self.artifact_reads += 1
                    if self.artifact_reads > 1:
                        self.artifact_expired = True
                return super().request(method, url, headers, body, timeout)

        with self.assertRaisesRegex(approval.ApprovalError, "artifact"):
            self.authorize_with(ExpiringArtifactTransport())

    def test_authorize_rechecks_live_approval_after_other_evidence(self) -> None:
        class DeletedApprovalTransport(ApprovalTransport):
            def __init__(self) -> None:
                super().__init__()
                self.comment_reads = 0

            def request(self, method, url, headers, body, timeout):
                if urllib.parse.urlsplit(url).path.endswith(
                    f"/issues/{PR_NUMBER}/comments"
                ):
                    self.comment_reads += 1
                    if self.comment_reads > 1:
                        self.ready_comment_present = False
                return super().request(method, url, headers, body, timeout)

        with self.assertRaisesRegex(approval.ApprovalError, "live GitHub Actions"):
            self.authorize_with(DeletedApprovalTransport())

    def test_authorize_refetches_main_validation_and_preflight_artifact(self) -> None:
        for field, value, message in (
            ("main", {"status": "in_progress"}, "main Validate PR"),
            ("artifact_id", ARTIFACT_ID + 1, "artifact ID"),
            ("artifact_digest", "sha256:" + "a" * 64, "artifact digest"),
            ("artifact_expired", True, "artifact"),
            (
                "artifact_workflow_run",
                {"head_branch": "main", "head_sha": FEATURE_HEAD, "id": PREFLIGHT_RUN},
                "artifact",
            ),
        ):
            with self.subTest(field=field):
                transport = ApprovalTransport()
                if field == "main":
                    transport.main_run.update(value)
                else:
                    setattr(transport, field, value)
                with self.assertRaisesRegex(approval.ApprovalError, message):
                    self.authorize_with(transport)

        transport = ApprovalTransport()
        transport.ready_comment_present = False
        with self.assertRaisesRegex(approval.ApprovalError, "readiness marker"):
            self.authorize_with(transport)

    def test_authorize_rejects_a_newer_pending_exact_sync_run(self) -> None:
        transport = ApprovalTransport()
        newer = copy.deepcopy(transport.source_run)
        newer.update(
            {
                "id": SOURCE_RUN + 1,
                "status": "in_progress",
                "conclusion": None,
            }
        )
        transport.source_runs.append(newer)
        with self.assertRaisesRegex(
            approval.ApprovalError, "not the newest exact-head run"
        ):
            self.authorize_with(transport)

    def test_authorize_rejects_rerun_before_remote_reads(self) -> None:
        transport = ApprovalTransport()
        with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(
            approval.ApprovalError, "not owner-authorized"
        ):
            approval.authorize(
                _event(),
                owner=OWNER,
                repository=REPOSITORY,
                actor=OWNER,
                triggering_actor=OWNER,
                run_attempt=2,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path(temp) / "approval.json",
                token=TOKEN,
                transport=transport,
            )
        self.assertEqual(transport.calls, [])

    def test_verify_artifact_requires_exact_receiver_and_observer_evidence(self) -> None:
        report = {
            "manifest_sha256": MANIFEST,
            "platform_version": VERSION,
            "preflight_run_attempt": 1,
            "preflight_run_id": PREFLIGHT_RUN,
            "preflight_artifact_id": ARTIFACT_ID,
            "preflight_artifact_digest": ARTIFACT_DIGEST,
            "release_commit": RELEASE,
            "repository": REPOSITORY,
            "schema_version": 2,
            "source_commit": SOURCE,
        }
        receiver = {
            "manifest_sha256": MANIFEST,
            "platform_version": VERSION,
            "result": "preflight-passed",
            "schema_version": 1,
        }
        attempt = {
            "attempt_id": f"gh-{PREFLIGHT_RUN}-1",
            "manifest_sha256": MANIFEST,
            "operation": "preflight",
            "platform_version": VERSION,
            "release_commit": RELEASE,
            "repository": REPOSITORY,
            "result": "success",
            "schema_version": 1,
            "source_commit": SOURCE,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "receiver.txt").write_text(
                json.dumps(receiver, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            (root / "attempt.txt").write_text(
                "B-UH Platform v2 guarded attempt report\n"
                + json.dumps(attempt, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            (root / "diagnostics.txt").write_text("bounded diagnostics\n")
            approval.verify_artifact(report, root)
            receiver["result"] = "verified"
            (root / "receiver.txt").write_text(json.dumps(receiver) + "\n")
            with self.assertRaisesRegex(
                approval.ApprovalError, "Receiver evidence"
            ):
                approval.verify_artifact(report, root)

    def test_verify_artifact_requires_matching_recovery_identity(self) -> None:
        report = {
            "manifest_sha256": MANIFEST,
            "platform_version": VERSION,
            "preflight_run_attempt": 1,
            "preflight_run_id": PREFLIGHT_RUN,
            "preflight_artifact_id": ARTIFACT_ID,
            "preflight_artifact_digest": ARTIFACT_DIGEST,
            "release_commit": RELEASE,
            "repository": REPOSITORY,
            "schema_version": 2,
            "source_commit": SOURCE,
        }
        recovery_digest = "d" * 64
        receiver = {
            "manifest_sha256": MANIFEST,
            "platform_version": VERSION,
            "recovery_transition_sha256": recovery_digest,
            "result": "preflight-passed",
            "schema_version": 1,
        }
        attempt = {
            "attempt_id": f"gh-{PREFLIGHT_RUN}-1",
            "manifest_sha256": MANIFEST,
            "operation": "preflight",
            "platform_version": VERSION,
            "release_commit": RELEASE,
            "release_recovery": {
                "baseline_platform_version": "0.5.6",
                "policy_id": "production-v0.5.6-published-gap-20260907",
                "purpose": "production-recovery",
                "release_count": 4,
                "sha256": recovery_digest,
            },
            "repository": REPOSITORY,
            "result": "success",
            "schema_version": 1,
            "source_commit": SOURCE,
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "receiver.txt").write_text(
                json.dumps(receiver, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            (root / "attempt.txt").write_text(
                "B-UH Platform v2 guarded attempt report\n"
                + json.dumps(attempt, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            (root / "diagnostics.txt").write_text(
                "bounded diagnostics\n", encoding="utf-8"
            )
            approval.verify_artifact(report, root)

            receiver["recovery_transition_sha256"] = "e" * 64
            (root / "receiver.txt").write_text(
                json.dumps(receiver, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(approval.ApprovalError, "does not agree"):
                approval.verify_artifact(report, root)

    def test_ready_rejects_an_unbound_artifact_without_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = approval._config(
                owner=OWNER,
                repository=REPOSITORY,
                version=VERSION,
                source_commit=SOURCE,
                release_commit=RELEASE,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path(temp) / "ready.json",
            )
            transport = ApprovalTransport()
            with self.assertRaisesRegex(
                approval.ApprovalError, "not bound"
            ):
                approval.ready(
                    config,
                    TOKEN,
                    number=PR_NUMBER,
                    feature_readiness=_published_readiness(),
                    main_validation_run_id=MAIN_RUN,
                    main_validation_run_attempt=1,
                    manifest_sha256=MANIFEST,
                    preflight_run_id=PREFLIGHT_RUN,
                    preflight_run_attempt=1,
                    preflight_artifact="wrong-name",
                    transport=transport,
                    sleeper=lambda _: None,
                    polls=1,
                )
            self.assertEqual(transport.calls, [])

    def test_ready_rejects_main_advancing_before_the_approval_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = approval._config(
                owner=OWNER,
                repository=REPOSITORY,
                version=VERSION,
                source_commit=SOURCE,
                release_commit=RELEASE,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path(temp) / "ready.json",
            )
            transport = ReadyTransport()
            transport.main_tip = "5" * 40
            with self.assertRaisesRegex(approval.ApprovalError, "main advanced"):
                approval.ready(
                    config,
                    TOKEN,
                    number=PR_NUMBER,
                    feature_readiness=_published_readiness(),
                    main_validation_run_id=MAIN_RUN,
                    main_validation_run_attempt=1,
                    manifest_sha256=MANIFEST,
                    preflight_run_id=PREFLIGHT_RUN,
                    preflight_run_attempt=1,
                    preflight_artifact=ARTIFACT,
                    transport=transport,
                    sleeper=lambda _: None,
                    polls=1,
                )

    def test_ready_rechecks_main_after_waiting_for_sync_validation(self) -> None:
        class AdvancingMainTransport(ReadyTransport):
            def __init__(self) -> None:
                super().__init__()
                self.main_reads = 0

            def request(self, method, url, headers, body, timeout):
                if urllib.parse.urlsplit(url).path.endswith("/git/ref/heads/main"):
                    self.main_reads += 1
                    if self.main_reads > 1:
                        self.main_tip = "5" * 40
                return super().request(method, url, headers, body, timeout)

        with tempfile.TemporaryDirectory() as temp:
            config = approval._config(
                owner=OWNER,
                repository=REPOSITORY,
                version=VERSION,
                source_commit=SOURCE,
                release_commit=RELEASE,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path(temp) / "ready.json",
            )
            with self.assertRaisesRegex(approval.ApprovalError, "main advanced"):
                approval.ready(
                    config,
                    TOKEN,
                    number=PR_NUMBER,
                    feature_readiness=_published_readiness(),
                    main_validation_run_id=MAIN_RUN,
                    main_validation_run_attempt=1,
                    manifest_sha256=MANIFEST,
                    preflight_run_id=PREFLIGHT_RUN,
                    preflight_run_attempt=1,
                    preflight_artifact=ARTIFACT,
                    transport=AdvancingMainTransport(),
                    sleeper=lambda _: None,
                    polls=1,
                )

    def test_ready_rechecks_artifact_after_waiting_for_sync_validation(self) -> None:
        class ExpiringArtifactTransport(ReadyTransport):
            def __init__(self) -> None:
                super().__init__()
                self.artifact_reads = 0

            def request(self, method, url, headers, body, timeout):
                if urllib.parse.urlsplit(url).path.endswith(
                    f"/actions/runs/{PREFLIGHT_RUN}/artifacts"
                ):
                    self.artifact_reads += 1
                    if self.artifact_reads > 1:
                        self.artifact_expired = True
                return super().request(method, url, headers, body, timeout)

        with tempfile.TemporaryDirectory() as temp:
            config = approval._config(
                owner=OWNER,
                repository=REPOSITORY,
                version=VERSION,
                source_commit=SOURCE,
                release_commit=RELEASE,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path(temp) / "ready.json",
            )
            with self.assertRaisesRegex(approval.ApprovalError, "artifact"):
                approval.ready(
                    config,
                    TOKEN,
                    number=PR_NUMBER,
                    feature_readiness=_published_readiness(),
                    main_validation_run_id=MAIN_RUN,
                    main_validation_run_attempt=1,
                    manifest_sha256=MANIFEST,
                    preflight_run_id=PREFLIGHT_RUN,
                    preflight_run_attempt=1,
                    preflight_artifact=ARTIFACT,
                    transport=ExpiringArtifactTransport(),
                    sleeper=lambda _: None,
                    polls=1,
                )

    def test_ready_rejects_a_newer_pending_sync_validation_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = approval._config(
                owner=OWNER,
                repository=REPOSITORY,
                version=VERSION,
                source_commit=SOURCE,
                release_commit=RELEASE,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path(temp) / "ready.json",
            )
            transport = ReadyTransport()
            newer = copy.deepcopy(transport.source_run)
            newer.update(
                {
                    "id": SOURCE_RUN + 1,
                    "status": "in_progress",
                    "conclusion": None,
                }
            )
            transport.source_runs.append(newer)
            with self.assertRaisesRegex(approval.ApprovalError, "timeout"):
                approval.ready(
                    config,
                    TOKEN,
                    number=PR_NUMBER,
                    feature_readiness=_published_readiness(),
                    main_validation_run_id=MAIN_RUN,
                    main_validation_run_attempt=1,
                    manifest_sha256=MANIFEST,
                    preflight_run_id=PREFLIGHT_RUN,
                    preflight_run_attempt=1,
                    preflight_artifact=ARTIFACT,
                    transport=transport,
                    sleeper=lambda _: None,
                    polls=1,
                )

    def test_ready_binds_feature_main_sync_and_exact_preflight_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = approval._config(
                owner=OWNER,
                repository=REPOSITORY,
                version=VERSION,
                source_commit=SOURCE,
                release_commit=RELEASE,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path(temp) / "ready.json",
            )
            transport = ReadyTransport()
            report = approval.ready(
                config,
                TOKEN,
                number=PR_NUMBER,
                feature_readiness=_published_readiness(),
                main_validation_run_id=MAIN_RUN,
                main_validation_run_attempt=1,
                manifest_sha256=MANIFEST,
                preflight_run_id=PREFLIGHT_RUN,
                preflight_run_attempt=1,
                preflight_artifact=ARTIFACT,
                transport=transport,
                sleeper=lambda _: None,
                polls=1,
            )
        ready = report["ready"]
        self.assertEqual(ready["feature_readiness"], _published_readiness())
        self.assertEqual(ready["main_validation_run_id"], MAIN_RUN)
        self.assertEqual(ready["sync_validation_run_id"], SOURCE_RUN)
        self.assertEqual(ready["preflight_artifact_id"], ARTIFACT_ID)
        self.assertEqual(ready["preflight_artifact_digest"], ARTIFACT_DIGEST)
        self.assertEqual(ready["approval_nonce"], approval._nonce(ready))
        self.assertTrue(report["approval_marker"].startswith(approval.APPROVAL_PREFIX))
        self.assertIn(
            ("POST", f"/repos/{REPOSITORY}/issues/{PR_NUMBER}/comments"),
            transport.calls,
        )

    def test_ready_reports_denied_comment_publication_without_response_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = approval._config(
                owner=OWNER,
                repository=REPOSITORY,
                version=VERSION,
                source_commit=SOURCE,
                release_commit=RELEASE,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path(temp) / "ready.json",
            )
            with self.assertRaises(sync.SyncPrError) as raised:
                approval.ready(
                    config,
                    TOKEN,
                    number=PR_NUMBER,
                    feature_readiness=_published_readiness(),
                    main_validation_run_id=MAIN_RUN,
                    main_validation_run_attempt=1,
                    manifest_sha256=MANIFEST,
                    preflight_run_id=PREFLIGHT_RUN,
                    preflight_run_attempt=1,
                    preflight_artifact=ARTIFACT,
                    transport=DeniedReadyTransport(),
                    sleeper=lambda _: None,
                    polls=1,
                )
        message = str(raised.exception)
        self.assertEqual(
            message,
            "GitHub denied readiness comment publication with HTTP 403",
        )
        self.assertNotIn(TOKEN, message)
        self.assertNotIn("private response body", message)
        self.assertNotIn("Authorization", message)

    def test_main_reports_expected_client_failure_safely_and_bounded(self) -> None:
        operation = "GitHub denied readiness comment publication with HTTP 403"
        unsafe_tail = "x" * 600 + "\nAuthorization: Bearer private-token\nprivate body"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temp, patch.object(
            approval,
            "ready",
            side_effect=sync.SyncPrError(operation + unsafe_tail),
        ):
            result = approval.main(
                _ready_main_arguments(Path(temp)),
                environment={"GITHUB_TOKEN": TOKEN},
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        rendered = stderr.getvalue()
        self.assertTrue(rendered.startswith("platform approval error: " + operation))
        summary = rendered.removeprefix("platform approval error: ").rstrip()
        self.assertLessEqual(len(summary), 500)
        self.assertNotIn("unexpected internal failure", rendered)
        self.assertNotIn(TOKEN, rendered)
        self.assertNotIn("Authorization", rendered)
        self.assertNotIn("private body", rendered)


if __name__ == "__main__":
    unittest.main()
