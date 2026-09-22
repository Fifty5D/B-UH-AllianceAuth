from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops/release"))
import platform_approval as approval  # noqa: E402
import reviewed_manual_deployment as manual  # noqa: E402


REPOSITORY = "Fifty5D/B-UH-AllianceAuth"
OWNER = "Fifty5D"
MAIN = "a" * 40
SOURCE = "b" * 40
RELEASE = "c" * 40
MANIFEST = "d" * 64
DIGEST = "sha256:" + "e" * 64
PREFLIGHT = 701
DEPLOY = 702
ARTIFACT = 703


def _repo() -> dict[str, str]:
    return {"full_name": REPOSITORY}


def _run(
    run_id: int,
    *,
    mode: str,
    status: str,
    conclusion: str | None,
    title: str,
) -> dict:
    return {
        "id": run_id,
        "run_attempt": 1,
        "event": "workflow_dispatch",
        "head_sha": MAIN,
        "head_branch": "main",
        "status": status,
        "conclusion": conclusion,
        "display_title": title,
        "path": manual.WORKFLOW,
        "repository": _repo(),
        "head_repository": _repo(),
        "actor": {"login": OWNER},
        "triggering_actor": {"login": OWNER},
        "created_at": "2026-09-22T12:05:00Z" if mode == "deploy" else "2026-09-22T12:00:00Z",
        "updated_at": "2026-09-22T12:05:00Z" if mode == "deploy" else "2026-09-22T12:04:00Z",
    }


class FakeClient:
    def __init__(self) -> None:
        self.preflight = _run(
            PREFLIGHT,
            mode="preflight",
            status="completed",
            conclusion="success",
            title=manual._run_title("preflight", "0.6.2", "none", "none"),
        )
        self.deploy = _run(
            DEPLOY,
            mode="deploy",
            status="in_progress",
            conclusion=None,
            title=manual._run_title("deploy", "0.6.2", str(PREFLIGHT), "1"),
        )
        self.dispatches = [self.deploy, self.preflight]

    def get(self, path, query=None):
        suffix = path.removeprefix(f"/repos/{REPOSITORY}/")
        if suffix == "git/ref/heads/main":
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": MAIN}}
        if suffix == "git/ref/heads/release/platform-v0.6.2":
            return {"ref": "refs/heads/release/platform-v0.6.2", "object": {"type": "commit", "sha": RELEASE}}
        if suffix == f"commits/{RELEASE}":
            return {"sha": RELEASE, "parents": [{"sha": SOURCE}]}
        if suffix == "actions/workflows/source-ci.yml/runs":
            run = {
                "id": 700,
                "run_number": 30,
                "run_attempt": 1,
                "event": "push",
                "head_sha": MAIN,
                "head_branch": "main",
                "status": "completed",
                "conclusion": "success",
                "path": ".github/workflows/source-ci.yml",
                "repository": _repo(),
                "head_repository": _repo(),
            }
            return {"total_count": 1, "workflow_runs": [run]}
        if suffix == f"actions/runs/{PREFLIGHT}":
            return copy.deepcopy(self.preflight)
        if suffix == "actions/workflows/deploy-platform-v2.yml/runs":
            return {
                "total_count": len(self.dispatches),
                "workflow_runs": copy.deepcopy(self.dispatches),
            }
        if suffix == f"actions/runs/{PREFLIGHT}/artifacts":
            artifact = {
                "id": ARTIFACT,
                "name": f"platform-v2-preflight-{PREFLIGHT}-1",
                "expired": False,
                "expires_at": "2099-09-22T12:00:00Z",
                "size_in_bytes": 4096,
                "digest": DIGEST,
                "workflow_run": {
                    "id": PREFLIGHT,
                    "head_sha": MAIN,
                    "head_branch": "main",
                },
            }
            return {"total_count": 1, "artifacts": [artifact]}
        raise AssertionError((path, query))


class ReviewedManualDeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeClient()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def authorize(self, **changes):
        values = {
            "owner": OWNER,
            "repository": REPOSITORY,
            "actor": OWNER,
            "triggering_actor": OWNER,
            "run_id": DEPLOY,
            "run_attempt": 1,
            "workflow_commit": MAIN,
            "version": "0.6.2",
            "source_commit": SOURCE,
            "release_commit": RELEASE,
            "manifest_sha256": MANIFEST,
            "preflight_run_id": PREFLIGHT,
            "preflight_run_attempt": 1,
            "preflight_artifact_id": ARTIFACT,
            "preflight_artifact_digest": DIGEST,
            "api_url": "https://api.github.com",
            "server_url": "https://github.com",
            "output": Path(self.temporary.name) / "approval.json",
            "token": "test-token",
            "client": self.client,
        }
        values.update(changes)
        return manual.authorize(**values)

    def test_binds_owner_dispatch_to_fresh_exact_preflight(self):
        report = self.authorize()
        self.assertEqual(report["authorization_mode"], "reviewed-manual-deployment")
        self.assertEqual(report["preflight_run_id"], PREFLIGHT)
        self.assertEqual(report["preflight_artifact_id"], ARTIFACT)
        self.assertEqual(report["release_commit"], RELEASE)
        self.assertEqual(report["workflow_commit"], MAIN)

    def test_rejects_workflow_rerun(self):
        with self.assertRaisesRegex(approval.ApprovalError, "identity"):
            self.authorize(run_attempt=2)

    def test_rejects_second_dispatch_for_same_preflight(self):
        duplicate = copy.deepcopy(self.client.deploy)
        duplicate["id"] = 704
        self.client.dispatches.append(duplicate)
        with self.assertRaisesRegex(approval.ApprovalError, "already been consumed"):
            self.authorize()

    def test_rejects_preflight_from_an_older_main(self):
        self.client.preflight["head_sha"] = "f" * 40
        with self.assertRaisesRegex(approval.ApprovalError, "preflight run identity"):
            self.authorize()

    def test_rejects_changed_artifact_digest(self):
        with self.assertRaisesRegex(approval.ApprovalError, "digest changed"):
            self.authorize(preflight_artifact_digest="sha256:" + "0" * 64)


if __name__ == "__main__":
    unittest.main()
