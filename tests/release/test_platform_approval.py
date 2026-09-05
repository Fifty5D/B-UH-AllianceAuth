from __future__ import annotations

import json
import copy
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any, Mapping


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
FIRST_PARENT = "5" * 40
PR_NUMBER = 31
PREFLIGHT_RUN = 555
SOURCE_RUN = 444
ARTIFACT = f"platform-v2-preflight-{PREFLIGHT_RUN}-1"
TOKEN = "test-token-that-must-not-be-rendered"


def _response(value: Any) -> sync.HttpResponse:
    return sync.HttpResponse(
        200, (json.dumps(value, separators=(",", ":")) + "\n").encode()
    )


def _run(*, preflight: bool) -> dict[str, Any]:
    if preflight:
        return {
            "actor": {"login": OWNER},
            "conclusion": "success",
            "event": "workflow_run",
            "head_repository": {"full_name": REPOSITORY},
            "head_sha": SOURCE,
            "id": PREFLIGHT_RUN,
            "path": ".github/workflows/auto-platform-release.yml@refs/heads/main",
            "repository": {"full_name": REPOSITORY},
            "run_attempt": 1,
            "status": "completed",
        }
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
    }


def _ready_payload() -> dict[str, Any]:
    payload = {
        "manifest_sha256": MANIFEST,
        "platform_version": VERSION,
        "preflight_artifact": ARTIFACT,
        "preflight_run_attempt": 1,
        "preflight_run_id": PREFLIGHT_RUN,
        "pull_request": PR_NUMBER,
        "release_commit": RELEASE,
        "repository": REPOSITORY,
        "schema_version": 1,
        "source_ci_run_attempt": 1,
        "source_ci_run_id": SOURCE_RUN,
        "source_commit": SOURCE,
    }
    payload["approval_nonce"] = approval._nonce(payload)
    return payload


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
        self.source_run = _run(preflight=False)
        self.associated_pulls = [_event()["pull_request"]]

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
                    "parents": [{"sha": FIRST_PARENT}, {"sha": RELEASE}],
                }
            )
        if suffix == f"compare/{MERGE}...main":
            return _response(
                {"status": "identical", "merge_base_commit": {"sha": MERGE}}
            )
        if suffix == f"issues/{PR_NUMBER}/comments":
            ready = _ready_payload()
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
            return _response(_run(preflight=True))
        if suffix == f"actions/runs/{SOURCE_RUN}":
            return _response(self.source_run)
        if suffix == f"commits/{RELEASE}/pulls":
            return _response(self.associated_pulls)
        if suffix == f"actions/runs/{PREFLIGHT_RUN}/artifacts":
            return _response(
                {
                    "artifacts": [
                        {
                            "expired": False,
                            "id": 990,
                            "name": ARTIFACT,
                            "size_in_bytes": 4096,
                        }
                    ]
                }
            )
        raise AssertionError(f"unexpected request: {method} {url}")

    @staticmethod
    def assert_get(method: str) -> None:
        if method != "GET":
            raise AssertionError(f"unexpected method: {method}")


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
            "release_commit": RELEASE,
            "repository": REPOSITORY,
            "schema_version": 1,
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
                    manifest_sha256=MANIFEST,
                    preflight_run_id=PREFLIGHT_RUN,
                    preflight_run_attempt=1,
                    preflight_artifact="wrong-name",
                    transport=transport,
                    sleeper=lambda _: None,
                    polls=1,
                )
            self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
