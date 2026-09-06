import hashlib
import io
import json
import struct
import unittest
import zipfile
import zlib
from copy import deepcopy

from ops.readiness import (
    PREVIEW_WORKFLOW,
    READINESS_WORKFLOW,
    REQUIRED_CHECK,
    ReadinessError,
    _manifest_from_zip,
    _verify_preview_zip,
    collect,
    needs_preview,
    validate,
    verify_published,
)


SHA = "a" * 40
OTHER_SHA = "b" * 40
REPOSITORY = "Fifty5D/B-UH-AllianceAuth"
PR = 42
RUN_ID = 1234
RUN_ATTEMPT = 2
MERGE_SHA = "d" * 40
BASE_SHA = "e" * 40
READINESS_RUN_ID = 5678
READINESS_RUN_ATTEMPT = 3


def screenshot_inventory():
    paths = [
        "screenshots/desktop/moon-tax.png",
        "screenshots/mobile/moon-tax.png",
    ]
    for theme in ("darkly", "flatly", "materia", "bootstrap", "bootstrap-dark"):
        for viewport in ("desktop", "mobile"):
            paths.append(f"screenshots/themes/{theme}/{viewport}/moon-tax.png")
    return sorted(paths)


def manifest():
    prefix = f"ui-preview-pr-{PR}-{SHA}-{RUN_ID}-{RUN_ATTEMPT}"
    return {
        "schema_version": 1,
        "repository": REPOSITORY,
        "pr": PR,
        "head_sha": SHA,
        "head_repository": REPOSITORY,
        "event": "pull_request",
        "workflow": PREVIEW_WORKFLOW,
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "evidence_artifact": prefix,
        "data": "synthetic-only",
        "network": "compose-internal-only",
        "screenshots": screenshot_inventory(),
        "playwright_report": "playwright-report/index.html",
    }


def artifact(name, artifact_id, digest="c" * 64, size=1024):
    return {
        "id": artifact_id,
        "name": name,
        "size_in_bytes": size,
        "expired": False,
        "run_id": RUN_ID,
        "head_sha": SHA,
        "digest": f"sha256:{digest}",
    }


def preview():
    prefix = f"ui-preview-pr-{PR}-{SHA}-{RUN_ID}-{RUN_ATTEMPT}"
    return {
        "workflow": PREVIEW_WORKFLOW,
        "repository": REPOSITORY,
        "display_title": f"Preview UI / PR #{PR} / {SHA} / synchronize / none",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "head_sha": SHA,
        "head_repository": REPOSITORY,
        "pr": PR,
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "manifest_artifact": artifact(f"{prefix}-manifest", 71),
        "evidence_artifact": artifact(prefix, 72),
        "manifest": manifest(),
        "archive_verified": True,
        "archive_sha256": "c" * 64,
    }


def review_evidence(
    latest=None,
    actionable=None,
    unresolved_serious=None,
    threads=None,
    issue_comments=None,
):
    snapshot = {
        "latest_reviews": latest or [],
        "latest_actionable_reviews": actionable or [],
        "unresolved_serious_reviews": unresolved_serious or [],
        "threads": threads or [],
        "issue_comments": issue_comments or [],
    }
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    return {
        "digest": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        **snapshot,
    }


def evidence(ui=False):
    return {
        "schema_version": 2,
        "repository": REPOSITORY,
        "pr": PR,
        "base_ref": "main",
        "head_sha": SHA,
        "head_repository": REPOSITORY,
        "observed_head_sha": SHA,
        "labels": ["codex", "needs-codex"],
        "changed_files": [
            "apps/example/static/example/app.js" if ui else "apps/example/models.py"
        ],
        "checks": [
            {
                "id": 55,
                "name": REQUIRED_CHECK,
                "status": "completed",
                "conclusion": "success",
                "head_sha": SHA,
                "app_slug": "github-actions",
                "run_id": 99,
                "run_attempt": 1,
                "workflow": ".github/workflows/source-ci.yml",
                "event": "pull_request",
                "repository": REPOSITORY,
                "pr": PR,
                "run_status": "completed",
                "run_conclusion": "success",
                "run_head_sha": SHA,
                "run_head_repository": REPOSITORY,
            }
        ],
        "preview": preview() if ui else None,
        "review_evidence": review_evidence(),
    }


def manifest_zip(value=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(value or manifest(), sort_keys=True, separators=(",", ":")),
        )
    return stream.getvalue()


def png(width, height):
    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    header = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    row = b"\x00" + b"\x00" * ((width + 7) // 8)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def evidence_zip(value=None, screenshot_payload=None):
    value = value or manifest()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(
            "manifest.json", json.dumps(value, sort_keys=True, separators=(",", ":"))
        )
        for path in value["screenshots"]:
            is_mobile = path.startswith("screenshots/mobile/") or "/mobile/" in path
            payload = png(390, 844) if is_mobile else png(1440, 1000)
            archive.writestr(
                path, payload if screenshot_payload is None else screenshot_payload
            )
        archive.writestr(value["playwright_report"], "<html></html>")
    return stream.getvalue()


def readiness_zip(value):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(
            "readiness.json",
            json.dumps(value, sort_keys=True, separators=(",", ":")),
        )
    return stream.getvalue()


class ReadinessTests(unittest.TestCase):
    def test_preview_relevance_includes_ui_and_preview_infrastructure(self):
        for path in (
            "apps/x/templates/x/index.html",
            "apps/x/static/x/app.js",
            "client/components/widget.jsx",
            "tests/browser/preview.spec.ts",
            ".github/workflows/ui-preview.yml",
            ".github/workflows/invalidate-readiness.yml",
            "ops/readiness.py",
            "platform/testenv/compose.yml",
            "platform/testauth/settings/browser.py",
            "tests/platform/test_readiness.py",
            "tests/platform/test_configuration.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(needs_preview([path], []))
        self.assertTrue(needs_preview(["docs/change.md"], ["ui-preview"]))
        self.assertFalse(needs_preview(["apps/x/models.py"], []))
        self.assertFalse(needs_preview(["setup/Upgrade-BUH-PlatformV2Receiver.ps1"], []))
        with self.assertRaises(ReadinessError):
            needs_preview("apps/x/templates/x/index.html", [])

    def test_non_ui_exact_head_can_be_ready(self):
        result = validate(evidence())
        self.assertTrue(result["ready"])
        self.assertEqual(result["required_check"]["name"], REQUIRED_CHECK)

    def test_missing_unknown_and_mistyped_top_level_evidence_fails(self):
        for mutate in (
            lambda value: value.pop("pr"),
            lambda value: value.update(extra=True),
            lambda value: value.update(pr=True),
            lambda value: value.update(labels="ready-for-work"),
            lambda value: value.update(changed_files={"apps/x/models.py"}),
        ):
            value = evidence()
            mutate(value)
            with self.assertRaises(ReadinessError):
                validate(value)

    def test_codex_label_stale_head_and_non_success_check_fail_closed(self):
        cases = []
        value = evidence()
        value["labels"] = []
        cases.append(value)
        value = evidence()
        value["labels"] = ["codex"]
        cases.append(value)
        value = evidence()
        value["labels"].append("ready-for-work")
        cases.append(value)
        value = evidence()
        value["observed_head_sha"] = OTHER_SHA
        cases.append(value)
        value = evidence()
        value["base_ref"] = "release-candidate"
        cases.append(value)
        value = evidence()
        value["checks"][0]["status"] = "in_progress"
        cases.append(value)
        value = evidence()
        value["checks"][0]["conclusion"] = "skipped"
        cases.append(value)
        value = evidence()
        value["checks"][0]["head_sha"] = OTHER_SHA
        cases.append(value)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ReadinessError):
                    validate(value)

    def test_only_the_compatible_github_actions_check_is_authoritative(self):
        cases = []
        value = evidence()
        value["checks"][0]["name"] = "Convenient green check"
        cases.append(value)
        value = evidence()
        value["checks"][0]["app_slug"] = "external-ci"
        cases.append(value)
        value = evidence()
        value["checks"][0]["workflow"] = ".github/workflows/other.yml"
        cases.append(value)
        value = evidence()
        value["checks"][0]["run_conclusion"] = "failure"
        cases.append(value)
        value = evidence()
        value["checks"].append(deepcopy(value["checks"][0]))
        cases.append(value)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ReadinessError):
                    validate(value)

    def test_ui_preview_is_bound_to_run_head_artifacts_and_manifest(self):
        result = validate(evidence(ui=True))
        self.assertTrue(result["preview_required"])
        self.assertEqual(result["preview"]["run_id"], RUN_ID)
        for field, bad_value in (
            ("workflow", ".github/workflows/other.yml"),
            ("event", "workflow_dispatch"),
            ("status", "in_progress"),
            ("conclusion", "failure"),
            ("head_sha", OTHER_SHA),
            ("pr", 41),
        ):
            value = evidence(ui=True)
            value["preview"][field] = bad_value
            with self.subTest(field=field):
                with self.assertRaises(ReadinessError):
                    validate(value)

    def test_preview_artifact_mismatch_expiry_digest_and_inventory_fail(self):
        mutations = (
            lambda item: item["preview"]["manifest_artifact"].update(expired=True),
            lambda item: item["preview"]["evidence_artifact"].update(run_id=999),
            lambda item: item["preview"]["evidence_artifact"].update(
                head_sha=OTHER_SHA
            ),
            lambda item: item["preview"]["evidence_artifact"].update(digest="bad"),
            lambda item: item["preview"].update(archive_verified=False),
            lambda item: item["preview"].update(archive_sha256="d" * 64),
            lambda item: item["preview"]["manifest"].update(head_sha=OTHER_SHA),
            lambda item: item["preview"]["manifest"].update(screenshots=[]),
            lambda item: item["preview"]["manifest"]["screenshots"].remove(
                "screenshots/themes/darkly/mobile/moon-tax.png"
            ),
        )
        for mutate in mutations:
            value = evidence(ui=True)
            mutate(value)
            with self.assertRaises(ReadinessError):
                validate(value)

    def test_review_snapshot_digest_and_unresolved_findings_fail_closed(self):
        review = {
            "id": 17,
            "author": "reviewer",
            "state": "CHANGES_REQUESTED",
            "submitted_at": "2026-09-06T12:00:00Z",
            "commit_id": SHA,
            "serious": False,
        }
        value = evidence()
        value["review_evidence"] = review_evidence(
            latest=[review], actionable=[review]
        )
        with self.assertRaisesRegex(ReadinessError, "changes-requested"):
            validate(value)

        thread = {
            "id": "PRRT_thread",
            "resolved": False,
            "comments": [
                {
                    "id": "PRRC_comment",
                    "updated_at": "2026-09-06T12:01:00Z",
                    "serious": True,
                }
            ],
        }
        value = evidence()
        value["review_evidence"] = review_evidence(threads=[thread])
        with self.assertRaisesRegex(ReadinessError, "serious"):
            validate(value)

        value = evidence()
        value["review_evidence"]["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ReadinessError, "digest"):
            validate(value)

        issue_comment = {
            "id": 91,
            "author": "work-reviewer",
            "created_at": "2026-09-06T12:00:00Z",
            "updated_at": "2026-09-06T12:01:00Z",
            "serious": True,
        }
        value = evidence()
        value["review_evidence"] = review_evidence(
            issue_comments=[issue_comment]
        )
        with self.assertRaisesRegex(ReadinessError, "serious issue-comment"):
            validate(value)

    def test_resolved_review_metadata_is_bound_without_comment_bodies(self):
        thread = {
            "id": "PRRT_thread",
            "resolved": True,
            "comments": [
                {
                    "id": "PRRC_comment",
                    "updated_at": "2026-09-06T12:01:00Z",
                    "serious": True,
                }
            ],
        }
        value = evidence()
        value["review_evidence"] = review_evidence(threads=[thread])
        result = validate(value)
        self.assertEqual(result["review_evidence"], value["review_evidence"])
        self.assertNotIn("body", json.dumps(result))

        for mutate in (
            lambda item: item["review_evidence"]["threads"][0].update(id=""),
            lambda item: item["review_evidence"]["threads"][0]["comments"][0].update(
                updated_at=None
            ),
            lambda item: item["review_evidence"]["threads"][0]["comments"][0].update(
                serious="true"
            ),
        ):
            value = evidence()
            value["review_evidence"] = review_evidence(threads=[deepcopy(thread)])
            mutate(value)
            with self.assertRaises(ReadinessError):
                validate(value)

    def test_manifest_zip_rejects_missing_duplicate_and_unsafe_entries(self):
        self.assertEqual(_manifest_from_zip(manifest_zip()), manifest())
        for names in ([], ["manifest.json", "extra.json"], ["../manifest.json"]):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                for name in names:
                    archive.writestr(name, "{}")
            with self.subTest(names=names):
                with self.assertRaises(ReadinessError):
                    _manifest_from_zip(stream.getvalue())

    def test_preview_zip_rejects_corrupt_and_truncated_png_screenshots(self):
        valid = png(1440, 1000)
        corrupt = bytearray(valid)
        corrupt[16] ^= 1
        for payload in (b"not-a-png", valid[:-5], bytes(corrupt)):
            with self.subTest(payload_size=len(payload)):
                with self.assertRaisesRegex(ReadinessError, "valid bounded PNG"):
                    _verify_preview_zip(
                        evidence_zip(screenshot_payload=payload), manifest()
                    )

        with self.assertRaisesRegex(ReadinessError, "desktop screenshot dimensions"):
            _verify_preview_zip(
                evidence_zip(screenshot_payload=png(390, 844)), manifest()
            )


class FakeGitHub:
    def __init__(self):
        self.manifest_zip = manifest_zip()
        self.evidence_zip = evidence_zip()
        self.final_sha = SHA
        self.run_status = "completed"
        self.run_conclusion = "success"
        self.head_repository = REPOSITORY
        self.base_ref = "main"
        self.labels = ["codex", "needs-codex"]
        self.ignored_preview_run = False
        self.changed_files = [".github/workflows/ui-preview.yml"]
        self.reported_changed_files = None
        self.reviews = []
        self.review_threads = []
        self.issue_comments = []
        self.source_run_pull_requests = [
            {"number": PR, "head": {"sha": SHA}}
        ]
        self.preview_run_pull_requests = [
            {"number": PR, "head": {"sha": SHA}}
        ]
        self.newer_source_run = None
        self.newer_preview_run = None
        self.manifest_digest = hashlib.sha256(self.manifest_zip).hexdigest()
        self.evidence_digest = hashlib.sha256(self.evidence_zip).hexdigest()

    def _pr(self):
        return {
            "number": PR,
            "state": "open",
            "head": {
                "sha": self.final_sha,
                "repo": {"full_name": self.head_repository},
            },
            "base": {
                "ref": self.base_ref,
                "repo": {"full_name": REPOSITORY},
            },
            "labels": [{"name": name} for name in self.labels],
            "changed_files": (
                len(self.changed_files)
                if self.reported_changed_files is None
                else self.reported_changed_files
            ),
        }

    def get_json(self, path, query=None):
        if path.endswith(f"/pulls/{PR}"):
            return self._pr()
        if path.endswith("/check-runs"):
            return {
                "check_runs": [
                    {
                        "id": 55,
                        "name": REQUIRED_CHECK,
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": SHA,
                        "details_url": f"https://github.com/{REPOSITORY}/actions/runs/99/job/5",
                        "app": {"slug": "github-actions"},
                    }
                ]
            }
        if path.endswith("/actions/runs/99"):
            return {
                "id": 99,
                "run_attempt": 1,
                "path": ".github/workflows/source-ci.yml",
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
                "repository": {"full_name": REPOSITORY},
                "head_sha": SHA,
                "head_repository": {"full_name": REPOSITORY},
                "pull_requests": deepcopy(self.source_run_pull_requests),
            }
        if path.endswith("/actions/workflows/source-ci.yml/runs"):
            runs = [
                {
                    "id": 99,
                    "run_attempt": 1,
                    "path": ".github/workflows/source-ci.yml",
                    "event": "pull_request",
                    "status": "completed",
                    "conclusion": "success",
                    "repository": {"full_name": REPOSITORY},
                    "head_sha": SHA,
                    "head_repository": {"full_name": REPOSITORY},
                    "pull_requests": deepcopy(self.source_run_pull_requests),
                }
            ]
            if self.newer_source_run is not None:
                runs.append(deepcopy(self.newer_source_run))
            return {"total_count": len(runs), "workflow_runs": runs}
        if "/actions/workflows/" in path:
            runs = [
                {
                    "id": RUN_ID,
                    "run_attempt": RUN_ATTEMPT,
                    "path": PREVIEW_WORKFLOW,
                    "repository": {"full_name": REPOSITORY},
                    "display_title": (
                        f"Preview UI / PR #{PR} / {SHA} / synchronize / none"
                    ),
                    "event": "pull_request",
                    "status": self.run_status,
                    "conclusion": self.run_conclusion,
                    "head_sha": SHA,
                    "head_repository": {"full_name": REPOSITORY},
                    "pull_requests": deepcopy(self.preview_run_pull_requests),
                }
            ]
            if self.ignored_preview_run:
                runs.append(
                    {
                        **runs[0],
                        "id": RUN_ID + 1,
                        "display_title": (
                            f"Preview UI / PR #{PR} / {SHA} / labeled / ready-for-work"
                        ),
                    }
                )
            if self.newer_preview_run is not None:
                runs.append(deepcopy(self.newer_preview_run))
            return {"total_count": len(runs), "workflow_runs": runs}
        if path.endswith(f"/actions/runs/{RUN_ID}/artifacts"):
            prefix = f"ui-preview-pr-{PR}-{SHA}-{RUN_ID}-{RUN_ATTEMPT}"
            return {
                "artifacts": [
                    {
                        **artifact(
                            prefix,
                            72,
                            digest=self.evidence_digest,
                            size=len(self.evidence_zip),
                        ),
                        "archive_download_url": "https://api.github.com/evidence.zip",
                        "workflow_run": {"id": RUN_ID, "head_sha": SHA},
                    },
                    {
                        **artifact(
                            f"{prefix}-manifest",
                            71,
                            digest=self.manifest_digest,
                            size=len(self.manifest_zip),
                        ),
                        "archive_download_url": "https://api.github.com/manifest.zip",
                        "workflow_run": {"id": RUN_ID, "head_sha": SHA},
                    },
                ]
            }
        raise AssertionError((path, query))

    def pages(self, path, key=None):
        if path.endswith("/files"):
            return [{"filename": value} for value in self.changed_files]
        if path.endswith("/reviews"):
            return self.reviews
        if path.endswith(f"/issues/{PR}/comments"):
            return deepcopy(self.issue_comments)
        raise AssertionError((path, key))

    def graphql(self, query, variables):
        return {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": self.review_threads,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }

    def download(self, url, maximum):
        self.assert_download_bound = maximum
        return self.manifest_zip if url.endswith("manifest.zip") else self.evidence_zip


class PublishedGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.labels = ["codex", "ready-for-work"]
        self.merged = True
        self.merger = "Anthony"
        self.merge_parents = [BASE_SHA, SHA]
        self.workflow_before = False
        self.workflow_after = True
        self.pointer_count = 1
        self.readiness_run_status = "completed"
        self.readiness_run_attempt = READINESS_RUN_ATTEMPT
        self.readiness_run_pull_requests = [
            {"number": PR, "head": {"sha": SHA}}
        ]
        self.artifact_expired = False
        self.preview_artifact_expired = False
        self.final_pr_mutation = None
        self.pr_calls = 0
        self.commit_pull_requests = None
        self.readiness_record = validate(evidence(ui=True))
        self.readiness_record["preview"][
            "manifest_artifact_digest"
        ] = f"sha256:{self.manifest_digest}"
        self.readiness_record["preview"][
            "evidence_artifact_digest"
        ] = f"sha256:{self.evidence_digest}"
        self.rebuild_readiness_artifact()

    def rebuild_readiness_artifact(self):
        self.readiness_zip = readiness_zip(self.readiness_record)
        self.readiness_digest = hashlib.sha256(self.readiness_zip).hexdigest()
        review_digest = self.readiness_record["review_evidence"]["digest"]
        self.pointer = {
            "schema_version": 2,
            "state": "qualified",
            "repository": REPOSITORY,
            "pr": PR,
            "head_sha": SHA,
            "review_digest": review_digest,
            "artifact_name": (
                f"pr-readiness-{PR}-{SHA}-{review_digest.removeprefix('sha256:')}"
            ),
            "artifact_id": 801,
            "artifact_digest": f"sha256:{self.readiness_digest}",
            "workflow": READINESS_WORKFLOW,
            "workflow_run_id": READINESS_RUN_ID,
            "workflow_run_attempt": READINESS_RUN_ATTEMPT,
        }

    def _pr_payload(self):
        return {
            "number": PR,
            "state": "closed" if self.merged else "open",
            "merged": self.merged,
            "merged_by": {"login": self.merger},
            "merge_commit_sha": MERGE_SHA,
            "head": {
                "sha": self.final_sha,
                "repo": {"full_name": self.head_repository},
            },
            "base": {
                "ref": self.base_ref,
                "repo": {"full_name": REPOSITORY},
            },
            "labels": [{"name": name} for name in self.labels],
            "changed_files": (
                len(self.changed_files)
                if self.reported_changed_files is None
                else self.reported_changed_files
            ),
        }

    def _pr(self):
        self.pr_calls += 1
        if self.final_pr_mutation is not None and self.pr_calls >= 2:
            self.final_pr_mutation(self)
        return self._pr_payload()

    def _commit_pr_payload(self):
        payload = self._pr_payload()
        for field in ("merged", "merged_by", "changed_files"):
            payload.pop(field)
        payload["merged_at"] = "2026-09-06T12:00:00Z"
        return payload

    def get_json(self, path, query=None):
        if path.endswith(f"/pulls/{PR}"):
            return self._pr()
        if path.endswith(f"/commits/{MERGE_SHA}"):
            return {
                "sha": MERGE_SHA,
                "parents": [{"sha": sha} for sha in self.merge_parents],
                "commit": {"tree": {"sha": "1" * 40}},
            }
        if path.endswith(f"/commits/{BASE_SHA}"):
            return {
                "sha": BASE_SHA,
                "parents": [{"sha": "f" * 40}],
                "commit": {"tree": {"sha": "2" * 40}},
            }
        if "/git/trees/" in path:
            is_merged = path.endswith("1" * 40)
            present = self.workflow_after if is_merged else self.workflow_before
            entries = []
            if present:
                entries.append(
                    {
                        "path": READINESS_WORKFLOW,
                        "type": "blob",
                        "sha": "3" * 40,
                    }
                )
            return {
                "sha": "1" * 40 if is_merged else "2" * 40,
                "truncated": False,
                "tree": entries,
            }
        if path.endswith(f"/actions/runs/{READINESS_RUN_ID}"):
            return {
                "id": READINESS_RUN_ID,
                "run_attempt": self.readiness_run_attempt,
                "path": READINESS_WORKFLOW,
                "event": "pull_request_target",
                "status": self.readiness_run_status,
                "conclusion": (
                    "success" if self.readiness_run_status == "completed" else None
                ),
                "repository": {"full_name": REPOSITORY},
                "head_sha": BASE_SHA,
                "head_repository": {"full_name": REPOSITORY},
                "pull_requests": deepcopy(self.readiness_run_pull_requests),
            }
        if path.endswith(f"/actions/runs/{RUN_ID}"):
            return {
                "id": RUN_ID,
                "run_attempt": RUN_ATTEMPT,
                "path": PREVIEW_WORKFLOW,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
                "repository": {"full_name": REPOSITORY},
                "head_sha": SHA,
                "head_repository": {"full_name": REPOSITORY},
                "pull_requests": deepcopy(self.preview_run_pull_requests),
            }
        if path.endswith("/actions/artifacts/801"):
            return {
                "id": 801,
                "name": self.pointer["artifact_name"],
                "size_in_bytes": len(self.readiness_zip),
                "expired": self.artifact_expired,
                "digest": f"sha256:{self.readiness_digest}",
                "archive_download_url": "https://api.github.com/readiness.zip",
                "workflow_run": {"id": READINESS_RUN_ID, "head_sha": BASE_SHA},
            }
        if path.endswith("/actions/artifacts/71"):
            prefix = f"ui-preview-pr-{PR}-{SHA}-{RUN_ID}-{RUN_ATTEMPT}"
            return {
                "id": 71,
                "name": f"{prefix}-manifest",
                "size_in_bytes": len(self.manifest_zip),
                "expired": self.preview_artifact_expired,
                "digest": f"sha256:{self.manifest_digest}",
                "workflow_run": {"id": RUN_ID, "head_sha": SHA},
            }
        if path.endswith("/actions/artifacts/72"):
            prefix = f"ui-preview-pr-{PR}-{SHA}-{RUN_ID}-{RUN_ATTEMPT}"
            return {
                "id": 72,
                "name": prefix,
                "size_in_bytes": len(self.evidence_zip),
                "expired": self.preview_artifact_expired,
                "digest": f"sha256:{self.evidence_digest}",
                "workflow_run": {"id": RUN_ID, "head_sha": SHA},
            }
        if path.endswith("/check-runs/55"):
            return {
                "id": 55,
                "name": REQUIRED_CHECK,
                "status": "completed",
                "conclusion": "success",
                "head_sha": SHA,
                "details_url": (
                    f"https://github.com/{REPOSITORY}/actions/runs/99/job/5"
                ),
                "app": {"slug": "github-actions"},
            }
        return super().get_json(path, query)

    def pages(self, path, key=None):
        if path.endswith(f"/commits/{SHA}/pulls"):
            if self.commit_pull_requests is not None:
                return deepcopy(self.commit_pull_requests)
            return [self._commit_pr_payload()]
        if path.endswith(f"/issues/{PR}/comments"):
            body = "<!-- buh-readiness-record:v2 -->\n" + json.dumps(
                self.pointer, sort_keys=True, separators=(",", ":")
            )
            return deepcopy(self.issue_comments) + [
                {"id": 900 + index, "user": {"login": "github-actions[bot]"}, "body": body}
                for index in range(self.pointer_count)
            ]
        return super().pages(path, key)

    def download(self, url, maximum):
        if url.endswith("readiness.zip"):
            self.assert_download_bound = maximum
            return self.readiness_zip
        return super().download(url, maximum)


class CollectionTests(unittest.TestCase):
    def test_collection_selects_and_binds_exact_github_evidence(self):
        result = collect(FakeGitHub(), REPOSITORY, PR, SHA)
        self.assertTrue(result["ready"])
        self.assertEqual(result["preview"]["run_id"], RUN_ID)
        self.assertRegex(result["review_evidence"]["digest"], r"^sha256:[0-9a-f]{64}$")

    def test_changed_file_enumeration_is_complete_and_below_the_api_cap(self):
        client = FakeGitHub()
        client.reported_changed_files = 2
        with self.assertRaisesRegex(ReadinessError, "count does not match"):
            collect(client, REPOSITORY, PR, SHA)

        client = FakeGitHub()
        client.reported_changed_files = 3000
        with self.assertRaisesRegex(ReadinessError, "3,000-file"):
            collect(client, REPOSITORY, PR, SHA)

    def test_collection_binds_review_id_state_thread_and_comment_metadata(self):
        client = FakeGitHub()
        client.reviews = [
            {
                "id": 7,
                "user": {"login": "Reviewer"},
                "state": "APPROVED",
                "submitted_at": "2026-09-06T12:00:00Z",
                "commit_id": SHA,
                "body": "Looks good.",
            }
        ]
        client.review_threads = [
            {
                "id": "PRRT_thread",
                "isResolved": True,
                "comments": {
                    "nodes": [
                        {
                            "id": "PRRC_comment",
                            "body": "[P1] fixed now",
                            "updatedAt": "2026-09-06T12:01:00Z",
                        }
                    ],
                    "pageInfo": {"hasNextPage": False},
                },
            }
        ]
        result = collect(client, REPOSITORY, PR, SHA)
        self.assertEqual(result["review_evidence"]["latest_reviews"][0]["id"], 7)
        self.assertEqual(
            result["review_evidence"]["latest_actionable_reviews"][0]["state"],
            "APPROVED",
        )
        self.assertEqual(result["review_evidence"]["threads"][0]["id"], "PRRT_thread")
        self.assertEqual(
            result["review_evidence"]["threads"][0]["comments"][0]["id"],
            "PRRC_comment",
        )
        self.assertNotIn("body", json.dumps(result))

        first_digest = result["review_evidence"]["digest"]
        client.review_threads[0]["comments"]["nodes"][0][
            "updatedAt"
        ] = "2026-09-06T12:02:00Z"
        self.assertNotEqual(
            collect(client, REPOSITORY, PR, SHA)["review_evidence"]["digest"],
            first_digest,
        )

    def test_latest_actionable_review_supersedes_an_older_change_request(self):
        client = FakeGitHub()
        client.reviews = [
            {
                "id": 7,
                "user": {"login": "Reviewer"},
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-09-06T11:00:00Z",
                "commit_id": SHA,
                "body": "Please fix this.",
            },
            {
                "id": 9,
                "user": {"login": "reviewer"},
                "state": "APPROVED",
                "submitted_at": "2026-09-06T12:00:00Z",
                "commit_id": SHA,
                "body": None,
            },
        ]
        result = collect(client, REPOSITORY, PR, SHA)
        self.assertEqual(result["review_evidence"]["latest_reviews"][0]["id"], 9)
        self.assertEqual(
            result["review_evidence"]["latest_actionable_reviews"][0]["state"],
            "APPROVED",
        )

    def test_collection_rejects_unresolved_serious_review_body_or_thread(self):
        client = FakeGitHub()
        client.reviews = [
            {
                "id": 8,
                "user": {"login": "Reviewer"},
                "state": "COMMENTED",
                "submitted_at": "2026-09-06T12:00:00Z",
                "commit_id": SHA,
                "body": "Security: do not ship this.",
            }
        ]
        with self.assertRaisesRegex(ReadinessError, "serious"):
            collect(client, REPOSITORY, PR, SHA)

    def test_issue_comment_findings_are_bound_and_require_explicit_clearance(self):
        client = FakeGitHub()
        client.issue_comments = [
            {
                "id": 91,
                "user": {"login": "work-reviewer"},
                "body": "[P1] The permission boundary is unsafe.",
                "created_at": "2026-09-06T12:00:00Z",
                "updated_at": "2026-09-06T12:00:00Z",
            }
        ]
        with self.assertRaisesRegex(ReadinessError, "serious issue-comment"):
            collect(client, REPOSITORY, PR, SHA)

        client.issue_comments[0]["body"] = "Finding cleared after the fix."
        client.issue_comments[0]["updated_at"] = "2026-09-06T12:05:00Z"
        result = collect(client, REPOSITORY, PR, SHA)
        self.assertEqual(
            result["review_evidence"]["issue_comments"],
            [
                {
                    "id": 91,
                    "author": "work-reviewer",
                    "created_at": "2026-09-06T12:00:00Z",
                    "updated_at": "2026-09-06T12:05:00Z",
                    "serious": False,
                }
            ],
        )
        self.assertNotIn("body", json.dumps(result))

    def test_benign_comment_cannot_hide_serious_review_but_approval_clears_it(self):
        client = FakeGitHub()
        client.reviews = [
            {
                "id": 8,
                "user": {"login": "Reviewer"},
                "state": "COMMENTED",
                "submitted_at": "2026-09-06T12:00:00Z",
                "commit_id": SHA,
                "body": "[P1] This blocks delivery.",
            },
            {
                "id": 9,
                "user": {"login": "reviewer"},
                "state": "COMMENTED",
                "submitted_at": "2026-09-06T12:01:00Z",
                "commit_id": SHA,
                "body": "One unrelated note.",
            },
        ]
        with self.assertRaisesRegex(ReadinessError, "serious"):
            collect(client, REPOSITORY, PR, SHA)

        client.reviews.append(
            {
                "id": 10,
                "user": {"login": "Reviewer"},
                "state": "APPROVED",
                "submitted_at": "2026-09-06T12:02:00Z",
                "commit_id": SHA,
                "body": "Resolved and approved.",
            }
        )
        result = collect(client, REPOSITORY, PR, SHA)
        self.assertEqual(result["review_evidence"]["unresolved_serious_reviews"], [])
        self.assertEqual(
            result["review_evidence"]["latest_actionable_reviews"][0]["state"],
            "APPROVED",
        )

        client.reviews = []
        client.review_threads = [
            {
                "id": "PRRT_thread",
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "id": "PRRC_comment",
                            "body": "[P1] still broken",
                            "updatedAt": "2026-09-06T12:01:00Z",
                        }
                    ],
                    "pageInfo": {"hasNextPage": False},
                },
            }
        ]
        with self.assertRaisesRegex(ReadinessError, "serious"):
            collect(client, REPOSITORY, PR, SHA)

    def test_newest_pending_preview_cannot_hide_behind_old_success(self):
        client = FakeGitHub()
        client.run_status = "in_progress"
        client.run_conclusion = None
        with self.assertRaisesRegex(ReadinessError, "pending"):
            collect(client, REPOSITORY, PR, SHA)

    def test_newer_deliberate_label_noop_does_not_hide_preview_evidence(self):
        client = FakeGitHub()
        client.ignored_preview_run = True
        result = collect(client, REPOSITORY, PR, SHA)
        self.assertTrue(result["ready"])
        self.assertEqual(result["preview"]["run_id"], RUN_ID)

    def test_duplicate_authoritative_checks_fail_closed(self):
        client = FakeGitHub()
        original = client.get_json

        def get_json(path, query=None):
            payload = original(path, query)
            if path.endswith("/check-runs"):
                duplicate = deepcopy(payload["check_runs"][0])
                duplicate["id"] += 1
                payload["check_runs"].append(duplicate)
            return payload

        client.get_json = get_json
        with self.assertRaisesRegex(ReadinessError, "exactly one"):
            collect(client, REPOSITORY, PR, SHA)

    def test_fork_and_missing_codex_label_are_rejected_before_collection(self):
        client = FakeGitHub()
        client.head_repository = "somebody/B-UH-AllianceAuth"
        with self.assertRaisesRegex(ReadinessError, "protected repository"):
            collect(client, REPOSITORY, PR, SHA)

        client = FakeGitHub()
        client.labels = []
        with self.assertRaisesRegex(ReadinessError, "Codex change"):
            collect(client, REPOSITORY, PR, SHA)

    def test_non_main_base_is_rejected_and_base_retargeting_fails_closed(self):
        client = FakeGitHub()
        client.base_ref = "release-candidate"
        with self.assertRaisesRegex(ReadinessError, "protected main branch"):
            collect(client, REPOSITORY, PR, SHA)

        client = FakeGitHub()
        calls = 0
        original = client.get_json

        def get_json(path, query=None):
            nonlocal calls
            if path.endswith(f"/pulls/{PR}"):
                calls += 1
                if calls == 2:
                    client.base_ref = "release-candidate"
            return original(path, query)

        client.get_json = get_json
        with self.assertRaisesRegex(ReadinessError, "base changed"):
            collect(client, REPOSITORY, PR, SHA)

    def test_malformed_preview_pr_association_fails_closed(self):
        client = FakeGitHub()
        original = client.get_json

        def get_json(path, query=None):
            payload = original(path, query)
            if "/actions/workflows/" in path:
                payload["workflow_runs"][0]["pull_requests"][0]["number"] = str(PR)
            return payload

        client.get_json = get_json
        with self.assertRaisesRegex(ReadinessError, "association"):
            collect(client, REPOSITORY, PR, SHA)

    def test_head_change_during_collection_fails_closed(self):
        client = FakeGitHub()
        calls = 0
        original = client.get_json

        def get_json(path, query=None):
            nonlocal calls
            if path.endswith(f"/pulls/{PR}"):
                calls += 1
                if calls == 2:
                    client.final_sha = OTHER_SHA
            return original(path, query)

        client.get_json = get_json
        with self.assertRaisesRegex(ReadinessError, "stale"):
            collect(client, REPOSITORY, PR, SHA)

    def test_downloaded_manifest_digest_is_verified(self):
        client = FakeGitHub()
        client.manifest_digest = "d" * 64
        with self.assertRaisesRegex(ReadinessError, "digest"):
            collect(client, REPOSITORY, PR, SHA)


class PublishedReadinessTests(unittest.TestCase):
    def verify(self, client=None, **kwargs):
        kwargs.setdefault("allowed_mergers", ["Anthony"])
        return verify_published(
            client or PublishedGitHub(),
            REPOSITORY,
            PR,
            SHA,
            MERGE_SHA,
            **kwargs,
        )

    def test_published_lineage_binds_merge_validation_readiness_and_preview(self):
        result = self.verify()
        self.assertEqual(result["attestation_mode"], "premerge-published")
        self.assertEqual(result["merger"], "Anthony")
        self.assertEqual(result["merge_source_commit"], MERGE_SHA)
        self.assertEqual(result["feature_pr"], {"number": PR, "head_sha": SHA})
        self.assertEqual(result["feature_validation"]["workflow_run_id"], 99)
        self.assertEqual(result["readiness"]["workflow_run_id"], READINESS_RUN_ID)
        self.assertEqual(
            result["review_digest"], result["readiness"]["review_digest"]
        )
        self.assertTrue(result["preview_required"])
        self.assertEqual(result["preview"]["run_id"], RUN_ID)

    def test_newer_pending_source_run_invalidates_retained_success(self):
        client = PublishedGitHub()
        client.newer_source_run = {
            "id": 100,
            "run_attempt": 1,
            "path": ".github/workflows/source-ci.yml",
            "event": "pull_request",
            "status": "in_progress",
            "conclusion": None,
            "repository": {"full_name": REPOSITORY},
            "head_sha": SHA,
            "head_repository": {"full_name": REPOSITORY},
            "pull_requests": [],
        }
        with self.assertRaisesRegex(ReadinessError, "not the newest exact-head run"):
            self.verify(client)

    def test_newer_pending_preview_run_invalidates_retained_success(self):
        client = PublishedGitHub()
        client.newer_preview_run = {
            "id": RUN_ID + 1,
            "run_attempt": 1,
            "path": PREVIEW_WORKFLOW,
            "display_title": (
                f"Preview UI / PR #{PR} / {SHA} / reopened / none"
            ),
            "event": "pull_request",
            "status": "in_progress",
            "conclusion": None,
            "repository": {"full_name": REPOSITORY},
            "head_sha": SHA,
            "head_repository": {"full_name": REPOSITORY},
            "pull_requests": [],
        }
        with self.assertRaisesRegex(ReadinessError, "not the newest exact-head run"):
            self.verify(client)

    def test_postmerge_empty_run_associations_use_exact_commit_pr_proof(self):
        client = PublishedGitHub()
        client.source_run_pull_requests = []
        client.readiness_run_pull_requests = []
        client.preview_run_pull_requests = []
        result = self.verify(client)
        self.assertEqual(result["attestation_mode"], "premerge-published")
        self.assertEqual(result["feature_validation"]["workflow_run_id"], 99)

        client = PublishedGitHub()
        client.source_run_pull_requests = []
        client.commit_pull_requests = []
        with self.assertRaisesRegex(ReadinessError, "feature commit"):
            self.verify(client)

    def test_nonempty_or_malformed_postmerge_run_associations_fail_closed(self):
        cases = (
            [{"number": PR + 1, "head": {"sha": SHA}}],
            [{"number": PR, "head": {"sha": OTHER_SHA}}],
            {},
        )
        for associations in cases:
            client = PublishedGitHub()
            client.source_run_pull_requests = associations
            with self.subTest(associations=associations):
                with self.assertRaisesRegex(ReadinessError, "association"):
                    self.verify(client)

    def test_merge_identity_and_qualified_labels_fail_closed(self):
        cases = []
        client = PublishedGitHub()
        client.merged = False
        cases.append(client)
        client = PublishedGitHub()
        client.labels.remove("ready-for-work")
        cases.append(client)
        client = PublishedGitHub()
        client.labels.append("needs-codex")
        cases.append(client)
        client = PublishedGitHub()
        client.head_repository = "someone/B-UH-AllianceAuth"
        cases.append(client)
        client = PublishedGitHub()
        client.merge_parents = [BASE_SHA]
        cases.append(client)
        client = PublishedGitHub()
        client.merge_parents[1] = OTHER_SHA
        cases.append(client)
        for client in cases:
            with self.subTest(labels=client.labels, parents=client.merge_parents):
                with self.assertRaises(ReadinessError):
                    self.verify(client)

    def test_every_mode_requires_an_explicit_authorized_merger(self):
        with self.assertRaisesRegex(ReadinessError, "explicit merger allowlist"):
            self.verify(allowed_mergers=[])

        client = PublishedGitHub()
        client.merger = "unexpected-user"
        with self.assertRaisesRegex(ReadinessError, "unauthorized"):
            self.verify(client)

    def test_pointer_workflow_and_artifact_are_strict_and_bounded(self):
        cases = []
        client = PublishedGitHub()
        client.pointer_count = 2
        cases.append(client)
        client = PublishedGitHub()
        client.pointer["state"] = "invalidated"
        cases.append(client)
        client = PublishedGitHub()
        client.pointer["unknown"] = True
        cases.append(client)
        client = PublishedGitHub()
        client.pointer["workflow_run_attempt"] += 1
        cases.append(client)
        client = PublishedGitHub()
        client.readiness_run_status = "in_progress"
        cases.append(client)
        client = PublishedGitHub()
        client.artifact_expired = True
        cases.append(client)
        client = PublishedGitHub()
        client.pointer["artifact_digest"] = "sha256:" + "0" * 64
        cases.append(client)
        for client in cases:
            with self.subTest(pointer=client.pointer):
                with self.assertRaises(ReadinessError):
                    self.verify(client)

        client = PublishedGitHub()
        client.preview_artifact_expired = True
        with self.assertRaisesRegex(ReadinessError, "retained preview"):
            self.verify(client)

    def test_readiness_zip_and_record_are_strictly_revalidated(self):
        client = PublishedGitHub()
        client.readiness_record["unexpected"] = True
        client.rebuild_readiness_artifact()
        with self.assertRaisesRegex(ReadinessError, "unknown"):
            self.verify(client)

        client = PublishedGitHub()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("readiness.json", "{}")
            archive.writestr("extra.txt", "not allowed")
        client.readiness_zip = stream.getvalue()
        client.readiness_digest = hashlib.sha256(client.readiness_zip).hexdigest()
        client.pointer["artifact_digest"] = f"sha256:{client.readiness_digest}"
        with self.assertRaisesRegex(ReadinessError, "unexpected entries"):
            self.verify(client)

    def test_review_change_and_final_label_race_fail_closed(self):
        client = PublishedGitHub()
        client.reviews = [
            {
                "id": 77,
                "user": {"login": "Reviewer"},
                "state": "COMMENTED",
                "submitted_at": "2026-09-06T12:00:00Z",
                "commit_id": SHA,
                "body": "A benign but new review.",
            }
        ]
        with self.assertRaisesRegex(ReadinessError, "review evidence differs"):
            self.verify(client)

        client = PublishedGitHub()
        client.issue_comments = [
            {
                "id": 92,
                "user": {"login": "work-reviewer"},
                "body": "A new benign but unreviewed note.",
                "created_at": "2026-09-06T12:10:00Z",
                "updated_at": "2026-09-06T12:10:00Z",
            }
        ]
        with self.assertRaisesRegex(ReadinessError, "review evidence differs"):
            self.verify(client)

        client = PublishedGitHub()
        client.final_pr_mutation = lambda value: value.labels.remove(
            "ready-for-work"
        )
        with self.assertRaisesRegex(ReadinessError, "readiness labels"):
            self.verify(client)

        client = PublishedGitHub()
        client.changed_files = ["apps/example/models.py"]
        client.readiness_record = validate(evidence(ui=False))
        client.rebuild_readiness_artifact()
        client.labels.append("ui-preview")
        with self.assertRaisesRegex(ReadinessError, "preview applicability"):
            self.verify(client)

    def test_published_verification_rechecks_complete_changed_file_evidence(self):
        client = PublishedGitHub()
        client.final_pr_mutation = lambda value: setattr(
            value, "reported_changed_files", 2
        )
        with self.assertRaisesRegex(ReadinessError, "count does not match"):
            self.verify(client)

        client = PublishedGitHub()
        client.reported_changed_files = 3000
        with self.assertRaisesRegex(ReadinessError, "3,000-file"):
            self.verify(client)

    def test_first_introduction_mode_directly_attests_exact_head(self):
        client = PublishedGitHub()
        client.pointer_count = 0
        client.source_run_pull_requests = []
        client.preview_run_pull_requests = []
        result = self.verify(
            client,
            allow_first_introduction=True,
            first_introduction_pr=PR,
            allowed_mergers=["anthony"],
        )
        self.assertEqual(
            result["attestation_mode"], "first-introduction-postmerge"
        )
        self.assertIsNone(result["readiness"])
        self.assertEqual(result["feature_validation"]["workflow_run_id"], 99)
        self.assertTrue(result["preview_required"])
        self.assertRegex(result["review_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_first_introduction_is_explicit_narrow_and_non_reusable(self):
        client = PublishedGitHub()
        client.pointer_count = 0
        with self.assertRaisesRegex(ReadinessError, "pointer"):
            self.verify(client)

        client = PublishedGitHub()
        client.pointer_count = 0
        with self.assertRaisesRegex(ReadinessError, "unauthorized"):
            self.verify(
                client,
                allow_first_introduction=True,
                first_introduction_pr=PR,
                allowed_mergers=["different-user"],
            )

        client = PublishedGitHub()
        client.pointer_count = 0
        client.workflow_after = False
        with self.assertRaisesRegex(ReadinessError, "does not add"):
            self.verify(
                client,
                allow_first_introduction=True,
                first_introduction_pr=PR,
                allowed_mergers=["Anthony"],
            )

        client = PublishedGitHub()
        client.pointer_count = 0
        client.workflow_before = True
        with self.assertRaisesRegex(ReadinessError, "pointer"):
            self.verify(
                client,
                allow_first_introduction=True,
                first_introduction_pr=PR,
                allowed_mergers=["Anthony"],
            )

        client = PublishedGitHub()
        client.pointer_count = 0
        with self.assertRaisesRegex(ReadinessError, "configured first-introduction PR"):
            self.verify(
                client,
                allow_first_introduction=True,
                allowed_mergers=["Anthony"],
            )

        client = PublishedGitHub()
        client.pointer_count = 0
        with self.assertRaisesRegex(ReadinessError, "configured first-introduction PR"):
            self.verify(
                client,
                allow_first_introduction=True,
                first_introduction_pr=PR + 1,
                allowed_mergers=["Anthony"],
            )


if __name__ == "__main__":
    unittest.main()
