"""Fail-closed, exact-head pull-request readiness policy.

Untrusted pull-request jobs create test and preview evidence with read-only
credentials.  The trusted ``pull_request_target`` workflow runs this module from
the exact base commit, reads GitHub metadata, parses (but never executes) the
bounded preview manifest, and builds the metadata-only record that must exist
before ``ready-for-work`` can be emitted for the current head.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


SHA_RE = re.compile(r"[0-9a-f]{40}")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
UI_PATH = re.compile(
    r"(^|/)(templates?|static|themes?|browser)(/|$)|"
    r"^\.github/workflows/(ui-preview|invalidate-readiness)\.ya?ml$|"
    r"^ops/readiness\.py$|"
    r"^platform/test(?:auth|env)/|"
    r"^tests/platform/test_(readiness|configuration)\.py$|"
    r"\.(?:css|scss|sass|less|js|jsx|mjs|cjs|ts|tsx|html)$",
    re.IGNORECASE,
)
SERIOUS_MARKER = re.compile(
    r"(?:\[(?:P0|P1)\]|\b(?:critical|high|serious|security)\b)", re.IGNORECASE
)
REVIEW_STATES = {
    "APPROVED",
    "CHANGES_REQUESTED",
    "COMMENTED",
    "DISMISSED",
    "PENDING",
}
ACTIONABLE_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
REQUIRED_CHECK = "Source test suite / Required source checks"
PROTECTED_BASE_REF = "main"
SOURCE_WORKFLOW = ".github/workflows/source-ci.yml"
PREVIEW_WORKFLOW = ".github/workflows/ui-preview.yml"
PREVIEW_WORKFLOW_FILE = "ui-preview.yml"
PREVIEW_ARTIFACT_LIMIT = 256 * 1024 * 1024
MANIFEST_ARTIFACT_LIMIT = 64 * 1024
MANIFEST_LIMIT = 16 * 1024
READINESS_WORKFLOW = ".github/workflows/invalidate-readiness.yml"
READINESS_POINTER_MARKER = "<!-- buh-readiness-record:v2 -->"
READINESS_ARTIFACT_LIMIT = 1024 * 1024
READINESS_RECORD_LIMIT = 512 * 1024
PULL_FILES_API_LIMIT = 3000


class ReadinessError(ValueError):
    """Readiness evidence is absent, stale, incomplete, or unsafe."""


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReadinessError(f"{context} is malformed")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    required: set[str],
    context: str,
    optional: set[str] | None = None,
) -> None:
    missing = required - set(value)
    unknown = set(value) - required - (optional or set())
    if missing:
        raise ReadinessError(f"{context} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise ReadinessError(f"{context} has unknown fields")


def _sha(value: Any, context: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ReadinessError(f"invalid {context} SHA")
    return value


def _positive_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReadinessError(f"invalid {context}")
    return value


def _strings(value: Any, context: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ReadinessError(f"{context} must be a list of strings")
    return value


def _metadata_string(value: Any, context: str, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ReadinessError(f"{context} is malformed")
    return value


def _review_digest(snapshot: Mapping[str, Any]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _validate_review_evidence(value: Any) -> dict[str, Any]:
    evidence = _mapping(value, "review evidence")
    _exact_keys(
        evidence,
        {
            "digest",
            "latest_reviews",
            "latest_actionable_reviews",
            "unresolved_serious_reviews",
            "threads",
            "issue_comments",
        },
        "review evidence",
    )

    def normalize_reviews(
        raw: Any, context: str, allowed: set[str]
    ) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            raise ReadinessError(f"{context} are malformed")
        normalized: list[dict[str, Any]] = []
        authors: set[str] = set()
        ids: set[int] = set()
        for item in raw:
            review = _mapping(item, context.removesuffix("s"))
            _exact_keys(
                review,
                {"id", "author", "state", "submitted_at", "commit_id", "serious"},
                context.removesuffix("s"),
            )
            review_id = _positive_int(review["id"], f"{context} id")
            author = _metadata_string(review["author"], f"{context} author", 100)
            state = review["state"]
            if state not in allowed:
                raise ReadinessError(f"{context} state is malformed")
            submitted_at = review["submitted_at"]
            if submitted_at is not None:
                submitted_at = _metadata_string(
                    submitted_at, f"{context} submission time", 64
                )
            commit_id = review["commit_id"]
            if commit_id is not None:
                commit_id = _sha(commit_id, f"{context} commit")
            if not isinstance(review["serious"], bool):
                raise ReadinessError(f"{context} serious marker is malformed")
            folded_author = author.casefold()
            if folded_author in authors or review_id in ids:
                raise ReadinessError(f"{context} contain duplicate identities")
            authors.add(folded_author)
            ids.add(review_id)
            normalized.append(
                {
                    "id": review_id,
                    "author": author,
                    "state": state,
                    "submitted_at": submitted_at,
                    "commit_id": commit_id,
                    "serious": review["serious"],
                }
            )
        expected = sorted(
            normalized,
            key=lambda item: (item["author"].casefold(), item["author"], item["id"]),
        )
        if normalized != expected:
            raise ReadinessError(f"{context} are not canonical")
        return normalized

    latest_reviews = normalize_reviews(
        evidence["latest_reviews"], "latest reviews", REVIEW_STATES
    )
    latest_actionable = normalize_reviews(
        evidence["latest_actionable_reviews"],
        "latest actionable reviews",
        ACTIONABLE_REVIEW_STATES,
    )
    unresolved_serious = normalize_reviews(
        evidence["unresolved_serious_reviews"],
        "unresolved serious reviews",
        REVIEW_STATES - {"APPROVED", "DISMISSED"},
    )
    if any(not review["serious"] for review in unresolved_serious):
        raise ReadinessError("unresolved serious review evidence is inconsistent")

    raw_threads = evidence["threads"]
    if not isinstance(raw_threads, list):
        raise ReadinessError("review threads are malformed")
    threads: list[dict[str, Any]] = []
    thread_ids: set[str] = set()
    for item in raw_threads:
        thread = _mapping(item, "review thread")
        _exact_keys(thread, {"id", "resolved", "comments"}, "review thread")
        thread_id = _metadata_string(thread["id"], "review thread id")
        if thread_id in thread_ids or not isinstance(thread["resolved"], bool):
            raise ReadinessError("review thread identity or resolution is malformed")
        thread_ids.add(thread_id)
        raw_comments = thread["comments"]
        if not isinstance(raw_comments, list) or not raw_comments:
            raise ReadinessError("review-thread comments are malformed")
        comments: list[dict[str, Any]] = []
        comment_ids: set[str] = set()
        for raw_comment in raw_comments:
            comment = _mapping(raw_comment, "review-thread comment")
            _exact_keys(
                comment,
                {"id", "updated_at", "serious"},
                "review-thread comment",
            )
            comment_id = _metadata_string(comment["id"], "review-thread comment id")
            updated_at = _metadata_string(
                comment["updated_at"], "review-thread comment update time", 64
            )
            if comment_id in comment_ids or not isinstance(comment["serious"], bool):
                raise ReadinessError("review-thread comment identity is malformed")
            comment_ids.add(comment_id)
            comments.append(
                {
                    "id": comment_id,
                    "updated_at": updated_at,
                    "serious": comment["serious"],
                }
            )
        expected_comments = sorted(comments, key=lambda item: item["id"])
        if comments != expected_comments:
            raise ReadinessError("review-thread comments are not canonical")
        threads.append(
            {"id": thread_id, "resolved": thread["resolved"], "comments": comments}
        )
    expected_threads = sorted(threads, key=lambda item: item["id"])
    if threads != expected_threads:
        raise ReadinessError("review threads are not canonical")

    raw_issue_comments = evidence["issue_comments"]
    if not isinstance(raw_issue_comments, list):
        raise ReadinessError("pull-request issue comments are malformed")
    issue_comments: list[dict[str, Any]] = []
    issue_comment_ids: set[int] = set()
    for item in raw_issue_comments:
        comment = _mapping(item, "pull-request issue comment")
        _exact_keys(
            comment,
            {"id", "author", "created_at", "updated_at", "serious"},
            "pull-request issue comment",
        )
        comment_id = _positive_int(comment["id"], "pull-request issue comment id")
        if comment_id in issue_comment_ids:
            raise ReadinessError("pull-request issue comments contain duplicate identities")
        issue_comment_ids.add(comment_id)
        author = _metadata_string(
            comment["author"], "pull-request issue comment author", 100
        )
        created_at = _metadata_string(
            comment["created_at"], "pull-request issue comment creation time", 64
        )
        updated_at = _metadata_string(
            comment["updated_at"], "pull-request issue comment update time", 64
        )
        serious = comment["serious"]
        if not isinstance(serious, bool):
            raise ReadinessError("pull-request issue comment serious marker is malformed")
        issue_comments.append(
            {
                "id": comment_id,
                "author": author,
                "created_at": created_at,
                "updated_at": updated_at,
                "serious": serious,
            }
        )
    expected_issue_comments = sorted(issue_comments, key=lambda item: item["id"])
    if issue_comments != expected_issue_comments:
        raise ReadinessError("pull-request issue comments are not canonical")

    snapshot = {
        "latest_reviews": latest_reviews,
        "latest_actionable_reviews": latest_actionable,
        "unresolved_serious_reviews": unresolved_serious,
        "threads": threads,
        "issue_comments": issue_comments,
    }
    digest = evidence["digest"]
    if digest != _review_digest(snapshot):
        raise ReadinessError("review evidence digest does not match its snapshot")
    if any(review["state"] == "CHANGES_REQUESTED" for review in latest_actionable):
        raise ReadinessError("an unresolved changes-requested review remains")
    if unresolved_serious:
        raise ReadinessError("an unresolved serious review finding remains")
    if any(
        review["serious"]
        and review["state"] not in {"APPROVED", "DISMISSED"}
        for review in latest_reviews
    ):
        raise ReadinessError("an unresolved serious review finding remains")
    if any(
        not thread["resolved"]
        and any(comment["serious"] for comment in thread["comments"])
        for thread in threads
    ):
        raise ReadinessError("an unresolved serious review finding remains")
    if any(comment["serious"] for comment in issue_comments):
        raise ReadinessError("an unresolved serious issue-comment finding remains")
    return {"digest": digest, **snapshot}


def needs_preview(paths: Sequence[str], labels: Sequence[str]) -> bool:
    if isinstance(paths, (str, bytes)) or isinstance(labels, (str, bytes)):
        raise ReadinessError("labels and changed paths must be string sequences")
    if not all(isinstance(value, str) for value in [*paths, *labels]):
        raise ReadinessError("labels and changed paths must be strings")
    return "ui-preview" in labels or any(UI_PATH.search(path) for path in paths)


def _preview_run_kind(title: Any, pr: int, head_sha: str) -> str:
    """Classify the deterministic Preview UI run title.

    Pull-request label events cannot be filtered at trigger time.  The preview
    workflow therefore records the triggering action and label in ``run-name``;
    readiness can ignore its deliberate no-op runs without mistaking them for
    the newest qualifying preview.
    """

    if not isinstance(title, str) or "\r" in title or "\n" in title:
        raise ReadinessError("UI preview run title is malformed")
    prefix = f"Preview UI / PR #{pr} / {head_sha} / "
    if not title.startswith(prefix):
        raise ReadinessError("UI preview run title is not bound to the pull request")
    trigger = title.removeprefix(prefix)
    if trigger in {"opened / none", "reopened / none", "synchronize / none"}:
        return "preview"
    if trigger == "labeled / ui-preview":
        return "preview"
    if trigger.startswith("labeled / ") and trigger != "labeled / ":
        return "ignored-label"
    raise ReadinessError("UI preview run title has an unexpected trigger")


def _validate_artifact(
    value: Any,
    *,
    expected_name: str,
    maximum_size: int,
    run_id: int,
    head_sha: str,
    context: str,
) -> dict[str, Any]:
    artifact = _mapping(value, context)
    _exact_keys(
        artifact,
        {"id", "name", "size_in_bytes", "expired", "run_id", "head_sha", "digest"},
        context,
    )
    artifact_id = _positive_int(artifact["id"], f"{context} id")
    size = artifact["size_in_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= maximum_size:
        raise ReadinessError(f"{context} has an invalid size")
    if artifact["name"] != expected_name:
        raise ReadinessError(f"{context} has an unexpected name")
    if artifact["expired"] is not False:
        raise ReadinessError(f"{context} is expired")
    if artifact["run_id"] != run_id:
        raise ReadinessError(f"{context} belongs to another workflow run")
    if artifact["head_sha"] != head_sha:
        raise ReadinessError(f"{context} belongs to another pull-request head")
    digest = artifact["digest"]
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ReadinessError(f"{context} lacks its GitHub artifact digest")
    return {"id": artifact_id, "digest": digest}


def validate_preview_manifest(
    value: Any,
    *,
    repository: str,
    pr: int,
    head_sha: str,
    head_repository: str,
    run_id: int,
    run_attempt: int,
    evidence_artifact: str,
    event: str = "pull_request",
) -> None:
    manifest = _mapping(value, "UI preview manifest")
    required = {
        "schema_version",
        "repository",
        "pr",
        "head_sha",
        "head_repository",
        "event",
        "workflow",
        "run_id",
        "run_attempt",
        "evidence_artifact",
        "data",
        "network",
        "screenshots",
        "playwright_report",
    }
    _exact_keys(manifest, required, "UI preview manifest")
    if manifest["schema_version"] != 1:
        raise ReadinessError("unsupported UI preview manifest schema")
    expected = {
        "repository": repository,
        "pr": pr,
        "head_sha": head_sha,
        "head_repository": head_repository,
        "event": event,
        "workflow": PREVIEW_WORKFLOW,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "evidence_artifact": evidence_artifact,
        "data": "synthetic-only",
        "network": "compose-internal-only",
        "playwright_report": "playwright-report/index.html",
    }
    for key, expected_value in expected.items():
        if manifest[key] != expected_value:
            raise ReadinessError(f"UI preview manifest {key} does not match")
    screenshots = _strings(manifest["screenshots"], "UI preview screenshots")
    if screenshots != sorted(set(screenshots)) or not screenshots:
        raise ReadinessError("UI preview screenshot inventory is not canonical")
    if not any(path.startswith("screenshots/desktop/") for path in screenshots):
        raise ReadinessError("UI preview lacks desktop screenshots")
    if not any(path.startswith("screenshots/mobile/") for path in screenshots):
        raise ReadinessError("UI preview lacks mobile screenshots")
    for theme in ("darkly", "flatly", "materia", "bootstrap", "bootstrap-dark"):
        for viewport in ("desktop", "mobile"):
            prefix = f"screenshots/themes/{theme}/{viewport}/"
            if not any(path.startswith(prefix) for path in screenshots):
                raise ReadinessError(f"UI preview lacks {theme} {viewport} evidence")
    if not all(
        re.fullmatch(r"screenshots/[A-Za-z0-9._/-]+\.png", path) and ".." not in path
        for path in screenshots
    ):
        raise ReadinessError("UI preview screenshot inventory contains an unsafe path")


def _validate_readiness_record(value: Mapping[str, Any]) -> dict[str, Any]:
    """Strictly validate the canonical record emitted by :func:`validate`."""

    _exact_keys(
        value,
        {
            "schema_version",
            "repository",
            "pr",
            "base_ref",
            "head_sha",
            "head_repository",
            "required_check",
            "preview_required",
            "preview",
            "review_evidence",
            "ready",
        },
        "readiness record",
    )
    if value["schema_version"] != 2:
        raise ReadinessError("unsupported readiness record schema")
    repository = value["repository"]
    if not isinstance(repository, str) or not REPOSITORY_RE.fullmatch(repository):
        raise ReadinessError("invalid readiness record repository identity")
    pr = _positive_int(value["pr"], "readiness record pull request number")
    if value["base_ref"] != PROTECTED_BASE_REF:
        raise ReadinessError("readiness record does not target the protected main branch")
    head = _sha(value["head_sha"], "readiness record pull-request head")
    if value["head_repository"] != repository:
        raise ReadinessError("readiness record head repository does not match")

    check = _mapping(value["required_check"], "readiness record required check")
    _exact_keys(
        check,
        {"name", "check_run_id", "workflow_run_id", "workflow_run_attempt"},
        "readiness record required check",
    )
    if check["name"] != REQUIRED_CHECK:
        raise ReadinessError("readiness record required check name does not match")
    normalized_check = {
        "name": REQUIRED_CHECK,
        "check_run_id": _positive_int(check["check_run_id"], "required check run id"),
        "workflow_run_id": _positive_int(
            check["workflow_run_id"], "required workflow run id"
        ),
        "workflow_run_attempt": _positive_int(
            check["workflow_run_attempt"], "required workflow run attempt"
        ),
    }

    preview_required = value["preview_required"]
    if not isinstance(preview_required, bool):
        raise ReadinessError("readiness record preview requirement is malformed")
    raw_preview = value["preview"]
    normalized_preview = None
    if preview_required:
        preview = _mapping(raw_preview, "readiness record preview")
        _exact_keys(
            preview,
            {
                "run_id",
                "run_attempt",
                "manifest_artifact_id",
                "manifest_artifact_digest",
                "evidence_artifact_id",
                "evidence_artifact_digest",
            },
            "readiness record preview",
        )

        def artifact_digest(raw: Any, context: str) -> str:
            if not isinstance(raw, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", raw
            ):
                raise ReadinessError(f"{context} is malformed")
            return raw

        normalized_preview = {
            "run_id": _positive_int(preview["run_id"], "preview run id"),
            "run_attempt": _positive_int(
                preview["run_attempt"], "preview run attempt"
            ),
            "manifest_artifact_id": _positive_int(
                preview["manifest_artifact_id"], "preview manifest artifact id"
            ),
            "manifest_artifact_digest": artifact_digest(
                preview["manifest_artifact_digest"],
                "preview manifest artifact digest",
            ),
            "evidence_artifact_id": _positive_int(
                preview["evidence_artifact_id"], "preview evidence artifact id"
            ),
            "evidence_artifact_digest": artifact_digest(
                preview["evidence_artifact_digest"],
                "preview evidence artifact digest",
            ),
        }
    elif raw_preview is not None:
        raise ReadinessError("readiness record has unexpected preview evidence")

    reviews = _validate_review_evidence(value["review_evidence"])
    if value["ready"] is not True:
        raise ReadinessError("readiness record is not qualified")
    return {
        "schema_version": 2,
        "repository": repository,
        "pr": pr,
        "base_ref": PROTECTED_BASE_REF,
        "head_sha": head,
        "head_repository": repository,
        "required_check": normalized_check,
        "preview_required": preview_required,
        "preview": normalized_preview,
        "review_evidence": reviews,
        "ready": True,
    }


def validate(evidence: dict[str, Any]) -> dict[str, Any]:
    """Validate collected evidence and return the exact-head readiness record."""

    value = _mapping(evidence, "readiness evidence")
    if "required_check" in value or "ready" in value:
        return _validate_readiness_record(value)
    required = {
        "schema_version",
        "repository",
        "pr",
        "base_ref",
        "head_sha",
        "head_repository",
        "observed_head_sha",
        "labels",
        "changed_files",
        "checks",
        "preview",
        "review_evidence",
    }
    _exact_keys(value, required, "readiness evidence")
    if value["schema_version"] != 2:
        raise ReadinessError("unsupported readiness evidence schema")
    repository = value["repository"]
    if not isinstance(repository, str) or not REPOSITORY_RE.fullmatch(repository):
        raise ReadinessError("invalid repository identity")
    pr = _positive_int(value["pr"], "pull request number")
    base_ref = value["base_ref"]
    if base_ref != PROTECTED_BASE_REF:
        raise ReadinessError("pull request does not target the protected main branch")
    head = _sha(value["head_sha"], "pull-request head")
    head_repository = value["head_repository"]
    if not isinstance(head_repository, str) or not REPOSITORY_RE.fullmatch(
        head_repository
    ):
        raise ReadinessError("invalid pull-request head repository")
    if head_repository != repository:
        raise ReadinessError("pull-request head is not in the protected repository")
    if value["observed_head_sha"] != head:
        raise ReadinessError("readiness evidence is stale")
    labels = _strings(value["labels"], "labels")
    paths = _strings(value["changed_files"], "changed paths")
    if "codex" not in labels:
        raise ReadinessError("pull request is not marked as a Codex change")
    if "ready-for-work" in labels or "needs-codex" not in labels:
        raise ReadinessError("readiness labels are not in the fail-closed publication state")

    checks = value["checks"]
    if not isinstance(checks, list) or len(checks) != 1:
        raise ReadinessError("exactly one authoritative check is required")
    check = _mapping(checks[0], "authoritative check")
    _exact_keys(
        check,
        {
            "id",
            "name",
            "status",
            "conclusion",
            "head_sha",
            "app_slug",
            "run_id",
            "run_attempt",
            "workflow",
            "event",
            "repository",
            "pr",
            "run_status",
            "run_conclusion",
            "run_head_sha",
            "run_head_repository",
        },
        "authoritative check",
    )
    check_id = _positive_int(check["id"], "authoritative check id")
    check_run_id = _positive_int(check["run_id"], "authoritative workflow run id")
    check_run_attempt = _positive_int(
        check["run_attempt"], "authoritative workflow run attempt"
    )
    if check["name"] != REQUIRED_CHECK:
        raise ReadinessError("the authoritative required check is missing")
    if check["app_slug"] != "github-actions":
        raise ReadinessError("the authoritative check is not GitHub Actions-authored")
    if (
        check["workflow"] != SOURCE_WORKFLOW
        or check["event"] != "pull_request"
        or check["repository"] != repository
        or check["run_head_repository"] != repository
        or check["pr"] != pr
    ):
        raise ReadinessError("the authoritative check workflow provenance is invalid")
    if check["run_head_sha"] != head:
        raise ReadinessError("the authoritative workflow run belongs to another head")
    if check["run_status"] != "completed" or check["run_conclusion"] != "success":
        raise ReadinessError("the authoritative workflow run is not successful")
    if check["status"] != "completed":
        raise ReadinessError("the authoritative check is pending")
    if check["conclusion"] != "success" or check["head_sha"] != head:
        raise ReadinessError("the authoritative check failed or belongs to another head")

    preview_required = needs_preview(paths, labels)
    preview_record = None
    preview = value["preview"]
    if preview_required:
        preview = _mapping(preview, "UI preview evidence")
        preview_fields = {
            "workflow",
            "repository",
            "display_title",
            "event",
            "status",
            "conclusion",
            "head_sha",
            "head_repository",
            "pr",
            "run_id",
            "run_attempt",
            "manifest_artifact",
            "evidence_artifact",
            "manifest",
            "archive_verified",
            "archive_sha256",
        }
        _exact_keys(preview, preview_fields, "UI preview evidence")
        run_id = _positive_int(preview["run_id"], "UI preview run id")
        run_attempt = _positive_int(preview["run_attempt"], "UI preview run attempt")
        if preview["workflow"] != PREVIEW_WORKFLOW or preview["event"] != "pull_request":
            raise ReadinessError("UI preview did not use the qualifying workflow event")
        if preview["repository"] != repository:
            raise ReadinessError("UI preview workflow repository does not match")
        if _preview_run_kind(preview["display_title"], pr, head) != "preview":
            raise ReadinessError("UI preview workflow run was a no-op")
        if preview["status"] != "completed":
            raise ReadinessError("UI preview is pending")
        if preview["conclusion"] != "success" or preview["head_sha"] != head:
            raise ReadinessError("UI preview failed or belongs to another head")
        if preview["pr"] != pr:
            raise ReadinessError("UI preview belongs to another pull request")
        if preview["archive_verified"] is not True:
            raise ReadinessError("UI preview archive was not independently verified")
        if not isinstance(preview["head_repository"], str) or not REPOSITORY_RE.fullmatch(
            preview["head_repository"]
        ):
            raise ReadinessError("UI preview has an invalid head repository")
        if preview["head_repository"] != head_repository:
            raise ReadinessError("UI preview belongs to another head repository")

        prefix = f"ui-preview-pr-{pr}-{head}-{run_id}-{run_attempt}"
        manifest_artifact = _validate_artifact(
            preview["manifest_artifact"],
            expected_name=f"{prefix}-manifest",
            maximum_size=MANIFEST_ARTIFACT_LIMIT,
            run_id=run_id,
            head_sha=head,
            context="UI preview manifest artifact",
        )
        evidence_artifact = _validate_artifact(
            preview["evidence_artifact"],
            expected_name=prefix,
            maximum_size=PREVIEW_ARTIFACT_LIMIT,
            run_id=run_id,
            head_sha=head,
            context="UI preview evidence artifact",
        )
        if preview["archive_sha256"] != evidence_artifact["digest"].removeprefix(
            "sha256:"
        ):
            raise ReadinessError("UI preview archive digest is not bound to the artifact")
        validate_preview_manifest(
            preview["manifest"],
            repository=repository,
            pr=pr,
            head_sha=head,
            head_repository=head_repository,
            run_id=run_id,
            run_attempt=run_attempt,
            evidence_artifact=prefix,
        )
        preview_record = {
            "run_id": run_id,
            "run_attempt": run_attempt,
            "manifest_artifact_id": manifest_artifact["id"],
            "manifest_artifact_digest": manifest_artifact["digest"],
            "evidence_artifact_id": evidence_artifact["id"],
            "evidence_artifact_digest": evidence_artifact["digest"],
        }
    elif preview is not None:
        raise ReadinessError("unexpected UI preview evidence")

    review_evidence = _validate_review_evidence(value["review_evidence"])

    return {
        "schema_version": 2,
        "repository": repository,
        "pr": pr,
        "base_ref": base_ref,
        "head_sha": head,
        "head_repository": head_repository,
        "required_check": {
            "name": REQUIRED_CHECK,
            "check_run_id": check_id,
            "workflow_run_id": check_run_id,
            "workflow_run_attempt": check_run_attempt,
        },
        "preview_required": preview_required,
        "preview": preview_record,
        "review_evidence": review_evidence,
        "ready": True,
    }


class GitHubClient:
    """Small bounded GitHub API client; every response remains untrusted."""

    def __init__(self, api_url: str, token: str) -> None:
        parsed = urllib.parse.urlparse(api_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ReadinessError("invalid GitHub API URL")
        if not token or "\r" in token or "\n" in token:
            raise ReadinessError("missing GitHub token")
        self.api_url = api_url.rstrip("/")
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "buh-readiness-v1",
        }

    def _url(self, path: str, query: Mapping[str, Any] | None = None) -> str:
        if not path.startswith("/") or ".." in path:
            raise ReadinessError("invalid GitHub API path")
        url = f"{self.api_url}{path}"
        return f"{url}?{urllib.parse.urlencode(query)}" if query else url

    def _read(self, request: urllib.request.Request, maximum: int) -> bytes:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read(maximum + 1)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ReadinessError("GitHub API request failed") from exc
        if len(payload) > maximum:
            raise ReadinessError("GitHub API response exceeded its bound")
        return payload

    def get_json(self, path: str, query: Mapping[str, Any] | None = None) -> Any:
        raw = self._read(
            urllib.request.Request(self._url(path, query), headers=self.headers),
            8 * 1024 * 1024,
        )
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReadinessError("GitHub API returned malformed JSON") from exc

    def pages(self, path: str, key: str | None = None) -> list[Any]:
        items: list[Any] = []
        for page in range(1, 101):
            payload = self.get_json(path, {"per_page": 100, "page": page})
            page_items = payload.get(key) if key and isinstance(payload, dict) else payload
            if not isinstance(page_items, list):
                raise ReadinessError("GitHub API pagination payload is malformed")
            items.extend(page_items)
            if len(page_items) < 100:
                return items
        raise ReadinessError("GitHub API pagination exceeded 100 pages")

    def graphql(self, query: str, variables: Mapping[str, Any]) -> Any:
        raw = self._read(
            urllib.request.Request(
                self._url("/graphql"),
                headers=self.headers,
                data=json.dumps({"query": query, "variables": variables}).encode(),
                method="POST",
            ),
            8 * 1024 * 1024,
        )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReadinessError("GitHub GraphQL returned malformed JSON") from exc
        if not isinstance(payload, dict) or payload.get("errors"):
            raise ReadinessError("GitHub GraphQL returned an error")
        return payload.get("data")

    def download(self, url: str, maximum: int) -> bytes:
        """Download one tiny ZIP without forwarding the token across a redirect."""

        parsed = urllib.parse.urlparse(url)
        api = urllib.parse.urlparse(self.api_url)
        if parsed.scheme != "https" or parsed.netloc != api.netloc:
            raise ReadinessError("artifact archive URL is outside the GitHub API")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(urllib.request.Request(url, headers=self.headers), timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise ReadinessError("GitHub artifact download failed") from exc
            location = exc.headers.get("Location", "")
        else:
            raise ReadinessError("GitHub artifact API did not return a download redirect")
        redirect = urllib.parse.urlparse(location)
        if redirect.scheme != "https" or not redirect.netloc or redirect.username:
            raise ReadinessError("GitHub artifact redirect is unsafe")
        return self._read(
            urllib.request.Request(
                location,
                headers={"Accept": "application/zip", "User-Agent": "buh-readiness-v1"},
            ),
            maximum,
        )


def _artifact(item: Any, run_id: int, head_sha: str) -> dict[str, Any]:
    value = _mapping(item, "GitHub artifact metadata")
    workflow_run = _mapping(value.get("workflow_run"), "GitHub artifact workflow run")
    artifact_run = workflow_run.get("id")
    artifact_head = workflow_run.get("head_sha")
    if artifact_run != run_id or artifact_head != head_sha:
        raise ReadinessError("GitHub artifact workflow provenance does not match")
    return {
        "id": value.get("id"),
        "name": value.get("name"),
        "size_in_bytes": value.get("size_in_bytes"),
        "expired": value.get("expired"),
        "run_id": artifact_run,
        "head_sha": artifact_head,
        "digest": value.get("digest"),
    }


def _manifest_from_zip(payload: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != "manifest.json":
                raise ReadinessError("UI preview manifest archive has unexpected entries")
            member = members[0]
            if member.flag_bits & 0x1 or member.file_size > MANIFEST_LIMIT:
                raise ReadinessError("UI preview manifest archive is unsafe")
            raw = archive.read(member)
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ReadinessError("UI preview manifest artifact is not a safe ZIP") from exc
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReadinessError("UI preview manifest is malformed") from exc
    if not isinstance(value, dict):
        raise ReadinessError("UI preview manifest is malformed")
    return value


def _verify_png(payload: bytes, path: str) -> None:
    """Validate bounded PNG structure and the expected viewport dimensions."""

    invalid = f"UI preview screenshot is not a valid bounded PNG: {path}"
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ReadinessError(invalid)
    offset = 8
    first = True
    saw_idat = False
    saw_iend = False
    width = height = 0
    while offset < len(payload):
        if len(payload) - offset < 12:
            raise ReadinessError(invalid)
        length = int.from_bytes(payload[offset : offset + 4], "big")
        chunk_type = payload[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        chunk_end = data_end + 4
        if (
            chunk_end > len(payload)
            or re.fullmatch(rb"[A-Za-z]{4}", chunk_type) is None
        ):
            raise ReadinessError(invalid)
        expected_crc = int.from_bytes(payload[data_end:chunk_end], "big")
        if zlib.crc32(chunk_type + payload[data_start:data_end]) != expected_crc:
            raise ReadinessError(invalid)
        if first:
            if chunk_type != b"IHDR" or length != 13:
                raise ReadinessError(invalid)
            header = payload[data_start:data_end]
            width = int.from_bytes(header[0:4], "big")
            height = int.from_bytes(header[4:8], "big")
            bit_depth, color_type, compression, filter_method, interlace = header[8:]
            legal_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                bit_depth not in legal_depths.get(color_type, set())
                or compression != 0
                or filter_method != 0
                or interlace not in {0, 1}
            ):
                raise ReadinessError(invalid)
            first = False
        elif chunk_type == b"IHDR":
            raise ReadinessError(invalid)
        if chunk_type == b"IDAT":
            saw_idat = True
        if chunk_type == b"IEND":
            if length != 0 or not saw_idat or chunk_end != len(payload):
                raise ReadinessError(invalid)
            saw_iend = True
            break
        offset = chunk_end
    if first or not saw_iend or not (1 <= width <= 32_768 and 200 <= height <= 32_768):
        raise ReadinessError(invalid)

    parts = PurePosixPath(path).parts
    if len(parts) >= 3 and parts[:2] in {
        ("screenshots", "desktop"),
        ("screenshots", "mobile"),
    }:
        viewport = parts[1]
    elif (
        len(parts) >= 5
        and parts[:2] == ("screenshots", "themes")
        and parts[3] in {"desktop", "mobile"}
    ):
        viewport = parts[3]
    else:
        raise ReadinessError("UI preview screenshot has no bounded viewport class")
    if viewport == "desktop" and width < 800:
        raise ReadinessError("UI preview desktop screenshot dimensions are malformed")
    if viewport == "mobile" and not 240 <= width <= 768:
        raise ReadinessError("UI preview mobile screenshot dimensions are malformed")


def _verify_preview_zip(payload: bytes, manifest: Mapping[str, Any]) -> None:
    """Inspect the evidence ZIP without extracting or executing any member."""

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if not members or len(members) > 10_000:
                raise ReadinessError("UI preview archive has an invalid member count")
            names = [member.filename for member in members]
            if len(names) != len(set(names)):
                raise ReadinessError("UI preview archive contains duplicate entries")
            total = 0
            files = set()
            screenshot_payloads: dict[str, bytes] = {}
            for member in members:
                path = PurePosixPath(member.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not path.parts
                    or "\\" in member.filename
                    or "\x00" in member.filename
                ):
                    raise ReadinessError("UI preview archive contains an unsafe path")
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ReadinessError("UI preview archive contains a symlink")
                file_type = stat.S_IFMT(mode)
                if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise ReadinessError("UI preview archive contains a special file")
                if member.is_dir():
                    continue
                if member.flag_bits & 0x1 or member.file_size > 64 * 1024 * 1024:
                    raise ReadinessError("UI preview archive contains an unsafe file")
                if member.filename == "manifest.json" and member.file_size > MANIFEST_LIMIT:
                    raise ReadinessError("UI preview archive manifest exceeds its bound")
                total += member.file_size
                if total > PREVIEW_ARTIFACT_LIMIT:
                    raise ReadinessError("UI preview archive exceeds its expanded size bound")
                files.add(member.filename)
                if member.filename.startswith("screenshots/") and member.filename.endswith(
                    ".png"
                ):
                    screenshot_payloads[member.filename] = archive.read(member)
            if "manifest.json" not in files:
                raise ReadinessError("UI preview archive lacks its manifest")
            archived_manifest = json.loads(archive.read("manifest.json"))
            if archived_manifest != manifest:
                raise ReadinessError("UI preview artifacts contain different manifests")
    except (zipfile.BadZipFile, RuntimeError, json.JSONDecodeError) as exc:
        raise ReadinessError("UI preview evidence artifact is not a safe ZIP") from exc
    expected_screenshots = set(manifest.get("screenshots", []))
    actual_screenshots = {
        name for name in files if name.startswith("screenshots/") and name.endswith(".png")
    }
    if actual_screenshots != expected_screenshots:
        raise ReadinessError("UI preview screenshot inventory does not match the archive")
    for screenshot in sorted(actual_screenshots):
        _verify_png(screenshot_payloads[screenshot], screenshot)
    if manifest.get("playwright_report") not in files:
        raise ReadinessError("UI preview archive lacks its Playwright report")


def _review_evidence(
    client: GitHubClient, repository: str, pr: int
) -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    latest_actionable: dict[str, dict[str, Any]] = {}
    reviews_by_author: dict[str, list[dict[str, Any]]] = {}
    review_ids: set[int] = set()
    for item in client.pages(f"/repos/{repository}/pulls/{pr}/reviews"):
        review = _mapping(item, "pull-request review")
        user = review.get("user")
        login = user.get("login") if isinstance(user, Mapping) else None
        review_id = review.get("id")
        if not isinstance(login, str) or isinstance(review_id, bool) or not isinstance(review_id, int):
            raise ReadinessError("pull-request review metadata is malformed")
        state = review.get("state")
        if state not in REVIEW_STATES:
            raise ReadinessError("pull-request review state is malformed")
        submitted_at = review.get("submitted_at")
        if submitted_at is not None:
            _metadata_string(submitted_at, "pull-request review submission time", 64)
        commit_id = review.get("commit_id")
        if commit_id is not None:
            _sha(commit_id, "pull-request review commit")
        body = review.get("body")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise ReadinessError("pull-request review body is malformed")
        normalized = {
            "id": _positive_int(review_id, "pull-request review id"),
            "author": _metadata_string(login, "pull-request review author", 100),
            "state": state,
            "submitted_at": submitted_at,
            "commit_id": commit_id,
            "serious": bool(SERIOUS_MARKER.search(body)),
        }
        if normalized["id"] in review_ids:
            raise ReadinessError("pull-request reviews contain duplicate identities")
        review_ids.add(normalized["id"])
        key = login.casefold()
        reviews_by_author.setdefault(key, []).append(normalized)
        if key not in latest or review_id > latest[key]["id"]:
            latest[key] = normalized
        if state in ACTIONABLE_REVIEW_STATES and (
            key not in latest_actionable or review_id > latest_actionable[key]["id"]
        ):
            latest_actionable[key] = normalized

    unresolved_serious: dict[str, dict[str, Any]] = {}
    for key, author_reviews in reviews_by_author.items():
        for review in sorted(author_reviews, key=lambda value: value["id"]):
            if review["state"] in {"APPROVED", "DISMISSED"}:
                unresolved_serious.pop(key, None)
            elif review["serious"]:
                unresolved_serious[key] = review

    normalized_issue_comments: list[dict[str, Any]] = []
    issue_comment_ids: set[int] = set()
    for item in client.pages(f"/repos/{repository}/issues/{pr}/comments"):
        comment = _mapping(item, "pull-request issue comment")
        user = comment.get("user")
        login = user.get("login") if isinstance(user, Mapping) else None
        body = comment.get("body")
        if (
            login == "github-actions[bot]"
            and isinstance(body, str)
            and (
                body == READINESS_POINTER_MARKER
                or body.startswith(f"{READINESS_POINTER_MARKER}\n")
            )
        ):
            continue
        comment_id = comment.get("id")
        if (
            not isinstance(login, str)
            or isinstance(comment_id, bool)
            or not isinstance(comment_id, int)
            or not isinstance(body, str)
            or len(body.encode("utf-8")) > 65_536
        ):
            raise ReadinessError("pull-request issue comment metadata is malformed")
        if comment_id in issue_comment_ids:
            raise ReadinessError("pull-request issue comments contain duplicate identities")
        issue_comment_ids.add(comment_id)
        created_at = _metadata_string(
            comment.get("created_at"), "pull-request issue comment creation time", 64
        )
        updated_at = _metadata_string(
            comment.get("updated_at"), "pull-request issue comment update time", 64
        )
        normalized_issue_comments.append(
            {
                "id": _positive_int(comment_id, "pull-request issue comment id"),
                "author": _metadata_string(
                    login, "pull-request issue comment author", 100
                ),
                "created_at": created_at,
                "updated_at": updated_at,
                "serious": bool(SERIOUS_MARKER.search(body)),
            }
        )

    owner, name = repository.split("/", 1)
    cursor = None
    normalized_threads: list[dict[str, Any]] = []
    query = """
      query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
        repository(owner: $owner, name: $name) {
          pullRequest(number: $number) {
            reviewThreads(first: 100, after: $cursor) {
              nodes {
                id
                isResolved
                comments(first: 100) {
                  nodes { id body updatedAt }
                  pageInfo { hasNextPage }
                }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
      }
    """
    for _ in range(100):
        data = client.graphql(
            query, {"owner": owner, "name": name, "number": pr, "cursor": cursor}
        )
        try:
            threads = data["repository"]["pullRequest"]["reviewThreads"]
            nodes = threads["nodes"]
            page_info = threads["pageInfo"]
        except (KeyError, TypeError) as exc:
            raise ReadinessError("review-thread evidence is malformed") from exc
        if not isinstance(nodes, list) or not isinstance(page_info, Mapping):
            raise ReadinessError("review-thread evidence is malformed")
        if not isinstance(page_info.get("hasNextPage"), bool):
            raise ReadinessError("review-thread pagination is malformed")
        for item in nodes:
            thread = _mapping(item, "review thread")
            thread_id = _metadata_string(thread.get("id"), "review thread id")
            resolved = thread.get("isResolved")
            if not isinstance(resolved, bool):
                raise ReadinessError("review thread resolution is malformed")
            comments = _mapping(thread.get("comments"), "review-thread comments")
            comment_page_info = _mapping(
                comments.get("pageInfo"), "review-thread page info"
            )
            if not isinstance(comment_page_info.get("hasNextPage"), bool):
                raise ReadinessError("review-thread pagination is malformed")
            if comment_page_info["hasNextPage"]:
                raise ReadinessError("review thread has more than 100 comments")
            comment_nodes = comments.get("nodes")
            if not isinstance(comment_nodes, list) or not comment_nodes:
                raise ReadinessError("review-thread comments are malformed")
            normalized_comments: list[dict[str, Any]] = []
            for raw_comment in comment_nodes:
                comment = _mapping(raw_comment, "review-thread comment")
                comment_id = _metadata_string(
                    comment.get("id"), "review-thread comment id"
                )
                updated_at = _metadata_string(
                    comment.get("updatedAt"), "review-thread comment update time", 64
                )
                body = comment.get("body")
                if not isinstance(body, str):
                    raise ReadinessError("review-thread comment body is malformed")
                normalized_comments.append(
                    {
                        "id": comment_id,
                        "updated_at": updated_at,
                        "serious": bool(SERIOUS_MARKER.search(body)),
                    }
                )
            normalized_threads.append(
                {
                    "id": thread_id,
                    "resolved": resolved,
                    "comments": sorted(normalized_comments, key=lambda value: value["id"]),
                }
            )
        if page_info["hasNextPage"] is not True:
            snapshot = {
                "latest_reviews": sorted(
                    latest.values(),
                    key=lambda value: (
                        value["author"].casefold(),
                        value["author"],
                        value["id"],
                    ),
                ),
                "latest_actionable_reviews": sorted(
                    latest_actionable.values(),
                    key=lambda value: (
                        value["author"].casefold(),
                        value["author"],
                        value["id"],
                    ),
                ),
                "unresolved_serious_reviews": sorted(
                    unresolved_serious.values(),
                    key=lambda value: (
                        value["author"].casefold(),
                        value["author"],
                        value["id"],
                    ),
                ),
                "threads": sorted(normalized_threads, key=lambda value: value["id"]),
                "issue_comments": sorted(
                    normalized_issue_comments, key=lambda value: value["id"]
                ),
            }
            return {"digest": _review_digest(snapshot), **snapshot}
        cursor = page_info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise ReadinessError("review-thread pagination is malformed")
    raise ReadinessError("review-thread pagination exceeded 100 pages")


def _labels(payload: Mapping[str, Any], context: str) -> list[str]:
    raw_labels = payload.get("labels")
    if not isinstance(raw_labels, list):
        raise ReadinessError(f"{context} labels are malformed")
    result = []
    for item in raw_labels:
        name = _mapping(item, f"{context} label").get("name")
        if not isinstance(name, str):
            raise ReadinessError(f"{context} label is malformed")
        result.append(name)
    return result


def _changed_file_count(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReadinessError(f"{context} is malformed")
    return value


def _changed_paths(
    client: GitHubClient,
    repository: str,
    pr: int,
    expected_count: Any,
) -> list[str]:
    expected = _changed_file_count(expected_count, "changed-file count")
    if expected >= PULL_FILES_API_LIMIT:
        raise ReadinessError(
            "changed-file evidence reaches GitHub's 3,000-file enumeration limit"
        )
    paths: list[str] = []
    for item in client.pages(f"/repos/{repository}/pulls/{pr}/files"):
        filename = _mapping(item, "changed file").get("filename")
        if not isinstance(filename, str):
            raise ReadinessError("changed-file evidence is malformed")
        paths.append(filename)
    if len(paths) != expected:
        raise ReadinessError("changed-file evidence count does not match pull request")
    if len(paths) != len(set(paths)):
        raise ReadinessError("changed-file evidence contains duplicate paths")
    return paths


def _one_exact_pr_association(
    value: Any, *, pr: int, head_sha: str, context: str
) -> bool:
    if not isinstance(value, list):
        raise ReadinessError(f"{context} PR associations are malformed")
    matches = 0
    for item in value:
        association = _mapping(item, f"{context} PR association")
        association_head = _mapping(
            association.get("head"), f"{context} PR association head"
        )
        number = association.get("number")
        association_sha = association_head.get("sha")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not isinstance(association_sha, str)
            or not SHA_RE.fullmatch(association_sha)
        ):
            raise ReadinessError(f"{context} PR association is malformed")
        if number == pr and association_sha == head_sha:
            matches += 1
    return matches == 1


def collect(
    client: GitHubClient,
    repository: str,
    pr: int,
    event_head: str,
    *,
    merged: bool = False,
) -> dict[str, Any]:
    """Collect and validate exact-head GitHub evidence.

    ``merged`` exists only for the explicitly gated first-introduction
    attestation.  It reads the same checks, previews, and reviews from a closed
    PR after independently verified merge identity; the public CLI never sets
    it for ordinary readiness publication.
    """

    if not REPOSITORY_RE.fullmatch(repository):
        raise ReadinessError("invalid repository identity")
    _positive_int(pr, "pull request number")
    _sha(event_head, "event head")
    pr_path = f"/repos/{repository}/pulls/{pr}"
    initial = _mapping(client.get_json(pr_path), "pull request")
    try:
        head = _sha(initial["head"]["sha"], "pull-request head")
        head_repository = initial["head"]["repo"]["full_name"]
        base_ref = initial["base"]["ref"]
        base_repository = initial["base"]["repo"]["full_name"]
    except (KeyError, TypeError) as exc:
        raise ReadinessError("pull-request branch metadata is malformed") from exc
    if initial.get("number") != pr:
        raise ReadinessError("pull request number does not match")
    if merged:
        if initial.get("state") != "closed" or initial.get("merged") is not True:
            raise ReadinessError("pull request is not merged and closed")
    elif initial.get("state") != "open":
        raise ReadinessError("pull request is not open")
    if head != event_head:
        raise ReadinessError("label event belongs to a stale pull-request head")
    if not isinstance(head_repository, str) or not REPOSITORY_RE.fullmatch(head_repository):
        raise ReadinessError("pull-request head repository is malformed")
    if head_repository != repository:
        raise ReadinessError("pull-request head is not in the protected repository")
    if base_repository != repository or base_ref != PROTECTED_BASE_REF:
        raise ReadinessError("pull request does not target the protected main branch")
    labels = _labels(initial, "pull-request")
    if "codex" not in labels:
        raise ReadinessError("pull request is not marked as a Codex change")
    if merged and (
        "ready-for-work" not in labels or "needs-codex" in labels
    ):
        raise ReadinessError("merged pull request lacks qualified readiness labels")

    changed_files = _changed_paths(
        client, repository, pr, initial.get("changed_files")
    )

    checks_payload = client.get_json(
        f"/repos/{repository}/commits/{head}/check-runs",
        {"check_name": REQUIRED_CHECK, "filter": "latest", "per_page": 100},
    )
    check_runs = checks_payload.get("check_runs") if isinstance(checks_payload, dict) else None
    if not isinstance(check_runs, list):
        raise ReadinessError("required-check evidence is malformed")
    if checks_payload.get("total_count", len(check_runs)) > len(check_runs):
        raise ReadinessError("required-check evidence is incomplete")
    exact_checks = [item for item in check_runs if item.get("name") == REQUIRED_CHECK]
    if len(exact_checks) != 1:
        raise ReadinessError("exactly one authoritative required check is required")
    selected_check = _mapping(exact_checks[0], "authoritative check run")
    details_url = selected_check.get("details_url")
    if not isinstance(details_url, str):
        raise ReadinessError("authoritative check details URL is malformed")
    run_match = re.fullmatch(
        rf"https://github\.com/{re.escape(repository)}/actions/runs/"
        r"([1-9][0-9]*)/job/[1-9][0-9]*",
        details_url,
    )
    app = selected_check.get("app")
    check = {
        "id": selected_check.get("id"),
        "name": selected_check.get("name"),
        "status": selected_check.get("status"),
        "conclusion": selected_check.get("conclusion"),
        "head_sha": selected_check.get("head_sha"),
        "app_slug": app.get("slug") if isinstance(app, Mapping) else None,
        "run_id": int(run_match.group(1)) if run_match else None,
    }
    if run_match:
        check_workflow_run = _mapping(
            client.get_json(
                f"/repos/{repository}/actions/runs/{int(run_match.group(1))}"
            ),
            "authoritative workflow run",
        )
        if check_workflow_run.get("id") != int(run_match.group(1)):
            raise ReadinessError("authoritative workflow run identity does not match")
        check_pull_requests = check_workflow_run.get("pull_requests")
        check_associated = check_pull_requests == [] and merged
        if not check_associated:
            check_associated = _one_exact_pr_association(
                check_pull_requests,
                pr=pr,
                head_sha=head,
                context="authoritative workflow",
            )
        check_repository = check_workflow_run.get("repository")
        check_head_repository = check_workflow_run.get("head_repository")
        check.update(
            {
                "run_attempt": check_workflow_run.get("run_attempt"),
                "workflow": check_workflow_run.get("path"),
                "event": check_workflow_run.get("event"),
                "repository": (
                    check_repository.get("full_name")
                    if isinstance(check_repository, Mapping)
                    else None
                ),
                "run_head_sha": check_workflow_run.get("head_sha"),
                "run_head_repository": (
                    check_head_repository.get("full_name")
                    if isinstance(check_head_repository, Mapping)
                    else None
                ),
                "pr": pr if check_associated else None,
                "run_status": check_workflow_run.get("status"),
                "run_conclusion": check_workflow_run.get("conclusion"),
            }
        )
    else:
        check.update(
            {
                "run_attempt": None,
                "workflow": None,
                "event": None,
                "repository": None,
                "pr": None,
                "run_status": None,
                "run_conclusion": None,
                "run_head_sha": None,
                "run_head_repository": None,
            }
        )
    preview = None
    if needs_preview(changed_files, labels):
        workflow = urllib.parse.quote(PREVIEW_WORKFLOW_FILE, safe="")
        run_payload = client.get_json(
            f"/repos/{repository}/actions/workflows/{workflow}/runs",
            {"event": "pull_request", "head_sha": head, "per_page": 100},
        )
        runs = run_payload.get("workflow_runs") if isinstance(run_payload, dict) else None
        if not isinstance(runs, list):
            raise ReadinessError("UI preview workflow-run evidence is malformed")
        if run_payload.get("total_count", len(runs)) > len(runs):
            raise ReadinessError("UI preview workflow-run evidence is incomplete")
        qualifying_runs = []
        for item in runs:
            run = _mapping(item, "UI preview workflow run")
            _positive_int(run.get("id"), "UI preview run id")
            _positive_int(run.get("run_attempt"), "UI preview run attempt")
            if run.get("path") != PREVIEW_WORKFLOW or run.get("event") != "pull_request":
                raise ReadinessError("UI preview workflow-run provenance is invalid")
            if run.get("head_sha") != head:
                raise ReadinessError("UI preview workflow query returned another head")
            run_repository = run.get("repository")
            if (
                not isinstance(run_repository, Mapping)
                or run_repository.get("full_name") != repository
            ):
                raise ReadinessError("UI preview workflow repository does not match")
            run_head = run.get("head_repository")
            if (
                not isinstance(run_head, Mapping)
                or run_head.get("full_name") != head_repository
            ):
                raise ReadinessError("UI preview run belongs to another head repository")
            run_pull_requests = run.get("pull_requests")
            run_associated = run_pull_requests == [] and merged
            if not run_associated:
                run_associated = _one_exact_pr_association(
                    run_pull_requests,
                    pr=pr,
                    head_sha=head,
                    context="UI preview workflow",
                )
            if not run_associated:
                raise ReadinessError("UI preview run has no exact PR association")
            kind = _preview_run_kind(run.get("display_title"), pr, head)
            if kind == "preview":
                qualifying_runs.append(run)
            elif kind != "ignored-label":
                raise ReadinessError("UI preview run has an unknown trigger")
        if not qualifying_runs:
            raise ReadinessError("UI preview run for the exact pull-request head is missing")
        run = max(qualifying_runs, key=lambda item: item["id"])
        run_id = _positive_int(run.get("id"), "UI preview run id")
        run_attempt = _positive_int(run.get("run_attempt"), "UI preview run attempt")
        run_head = run.get("head_repository")
        run_head_repository = run_head.get("full_name") if isinstance(run_head, Mapping) else None
        prefix = f"ui-preview-pr-{pr}-{head}-{run_id}-{run_attempt}"
        artifacts_payload = client.get_json(
            f"/repos/{repository}/actions/runs/{run_id}/artifacts", {"per_page": 100}
        )
        artifacts = artifacts_payload.get("artifacts") if isinstance(artifacts_payload, dict) else None
        if not isinstance(artifacts, list):
            raise ReadinessError("UI preview artifact evidence is malformed")
        if artifacts_payload.get("total_count", len(artifacts)) > len(artifacts):
            raise ReadinessError("UI preview artifact evidence is incomplete")
        by_name: dict[str, list[Mapping[str, Any]]] = {}
        for item in artifacts:
            artifact = _mapping(item, "UI preview artifact")
            name = artifact.get("name")
            if isinstance(name, str):
                by_name.setdefault(name, []).append(artifact)
        evidence_matches = by_name.get(prefix, [])
        manifest_matches = by_name.get(f"{prefix}-manifest", [])
        if len(evidence_matches) != 1 or len(manifest_matches) != 1:
            raise ReadinessError("exact UI preview artifacts are missing or duplicated")
        manifest_metadata = manifest_matches[0]
        manifest_size = manifest_metadata.get("size_in_bytes")
        if (
            isinstance(manifest_size, bool)
            or not isinstance(manifest_size, int)
            or not 0 < manifest_size <= MANIFEST_ARTIFACT_LIMIT
        ):
            raise ReadinessError("UI preview manifest artifact exceeds its size bound")
        manifest_zip = client.download(
            str(manifest_metadata.get("archive_download_url", "")),
            MANIFEST_ARTIFACT_LIMIT,
        )
        if manifest_metadata.get("digest") != f"sha256:{hashlib.sha256(manifest_zip).hexdigest()}":
            raise ReadinessError("UI preview manifest artifact digest does not match")
        manifest = _manifest_from_zip(manifest_zip)
        evidence_metadata = evidence_matches[0]
        evidence_size = evidence_metadata.get("size_in_bytes")
        if (
            isinstance(evidence_size, bool)
            or not isinstance(evidence_size, int)
            or not 0 < evidence_size <= PREVIEW_ARTIFACT_LIMIT
        ):
            raise ReadinessError("UI preview evidence artifact exceeds its size bound")
        evidence_zip = client.download(
            str(evidence_metadata.get("archive_download_url", "")),
            PREVIEW_ARTIFACT_LIMIT,
        )
        evidence_sha256 = hashlib.sha256(evidence_zip).hexdigest()
        if evidence_metadata.get("digest") != f"sha256:{evidence_sha256}":
            raise ReadinessError("UI preview evidence artifact digest does not match")
        _verify_preview_zip(evidence_zip, manifest)
        preview = {
            "workflow": run.get("path"),
            "repository": run.get("repository", {}).get("full_name"),
            "display_title": run.get("display_title"),
            "event": run.get("event"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "head_sha": run.get("head_sha"),
            "head_repository": run_head_repository,
            "pr": pr,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "manifest_artifact": _artifact(manifest_metadata, run_id, head),
            "evidence_artifact": _artifact(evidence_metadata, run_id, head),
            "manifest": manifest,
            "archive_verified": True,
            "archive_sha256": evidence_sha256,
        }

    review_evidence = _review_evidence(client, repository, pr)
    final = _mapping(client.get_json(pr_path), "final pull request")
    try:
        observed_head = final["head"]["sha"]
    except (KeyError, TypeError) as exc:
        raise ReadinessError("final pull-request head metadata is malformed") from exc
    if final.get("number") != pr:
        raise ReadinessError("pull request number changed during readiness validation")
    if merged:
        if final.get("state") != "closed" or final.get("merged") is not True:
            raise ReadinessError("merged pull request changed state during validation")
    elif final.get("state") != "open":
        raise ReadinessError("pull request closed during readiness validation")
    try:
        final_head_repository = final["head"]["repo"]["full_name"]
        final_base_ref = final["base"]["ref"]
        final_base_repository = final["base"]["repo"]["full_name"]
    except (KeyError, TypeError) as exc:
        raise ReadinessError("final pull-request repository metadata is malformed") from exc
    if final_head_repository != head_repository:
        raise ReadinessError("pull-request head repository changed during validation")
    if final_base_ref != base_ref or final_base_repository != base_repository:
        raise ReadinessError("pull-request base changed during readiness validation")
    final_labels = _labels(final, "final pull-request")
    if merged:
        if "ready-for-work" not in final_labels or "needs-codex" in final_labels:
            raise ReadinessError("merged pull-request readiness labels changed")
        # ``validate`` also enforces the transient fail-closed label state used
        # during pre-merge publication.  The record itself is label-free, so
        # translate only that already-verified state while retaining ui-preview.
        final_labels = [
            label for label in final_labels if label != "ready-for-work"
        ] + ["needs-codex"]
    return validate(
        {
            "schema_version": 2,
            "repository": repository,
            "pr": pr,
            "base_ref": base_ref,
            "head_sha": head,
            "head_repository": head_repository,
            "observed_head_sha": observed_head,
            "labels": final_labels,
            "changed_files": changed_files,
            "checks": [check],
            "preview": preview,
            "review_evidence": review_evidence,
        }
    )


def _merged_feature_pr(
    value: Any,
    *,
    repository: str,
    pr: int,
    feature_head: str,
    merge_source_commit: str,
    context: str,
) -> dict[str, Any]:
    pull = _mapping(value, context)
    try:
        head_sha = pull["head"]["sha"]
        head_repository = pull["head"]["repo"]["full_name"]
        base_ref = pull["base"]["ref"]
        base_repository = pull["base"]["repo"]["full_name"]
    except (KeyError, TypeError) as exc:
        raise ReadinessError(f"{context} repository metadata is malformed") from exc
    if pull.get("number") != pr:
        raise ReadinessError(f"{context} number does not match")
    if pull.get("state") != "closed" or pull.get("merged") is not True:
        raise ReadinessError("feature pull request is not merged and closed")
    if head_sha != feature_head or head_repository != repository:
        raise ReadinessError("feature pull-request head identity does not match")
    if base_ref != PROTECTED_BASE_REF or base_repository != repository:
        raise ReadinessError("feature pull request does not target this repository's main")
    if pull.get("merge_commit_sha") != merge_source_commit:
        raise ReadinessError("feature pull-request merge commit does not match")
    merged_by = pull.get("merged_by")
    merged_by_login = (
        merged_by.get("login") if isinstance(merged_by, Mapping) else None
    )
    merged_by_login = _metadata_string(
        merged_by_login, f"{context} merger login", 100
    )
    labels = _labels(pull, context)
    changed_file_count = _changed_file_count(
        pull.get("changed_files"), f"{context} changed-file count"
    )
    if len(labels) != len(set(labels)):
        raise ReadinessError(f"{context} labels contain duplicates")
    if "codex" not in labels or "ready-for-work" not in labels:
        raise ReadinessError("feature pull request lacks its qualified readiness labels")
    if "needs-codex" in labels:
        raise ReadinessError("feature pull request remains marked needs-codex")
    return {
        "head_sha": head_sha,
        "head_repository": head_repository,
        "base_ref": base_ref,
        "base_repository": base_repository,
        "merge_commit_sha": merge_source_commit,
        "merged_by": merged_by_login,
        "labels": sorted(labels),
        "changed_file_count": changed_file_count,
    }


def _verified_feature_commit_pr_association(
    client: GitHubClient,
    repository: str,
    pr: int,
    feature_head: str,
    merge_source_commit: str,
    expected: Mapping[str, Any],
) -> None:
    """Bind a merged PR to its exact feature commit independently of run metadata."""

    associations = client.pages(
        f"/repos/{repository}/commits/{feature_head}/pulls"
    )
    if len(associations) != 1:
        raise ReadinessError(
            "feature commit does not have one exact pull-request association"
        )
    associated = _mapping(
        associations[0], "feature commit pull-request association"
    )
    try:
        head_sha = associated["head"]["sha"]
        head_repository = associated["head"]["repo"]["full_name"]
        base_ref = associated["base"]["ref"]
        base_repository = associated["base"]["repo"]["full_name"]
    except (KeyError, TypeError) as exc:
        raise ReadinessError(
            "feature commit pull-request association is malformed"
        ) from exc
    labels = _labels(associated, "feature commit pull-request association")
    if (
        associated.get("number") != pr
        or associated.get("state") != "closed"
        or head_sha != feature_head
        or head_repository != repository
        or base_ref != PROTECTED_BASE_REF
        or base_repository != repository
        or associated.get("merge_commit_sha") != merge_source_commit
        or sorted(labels) != expected["labels"]
    ):
        raise ReadinessError("feature commit pull-request association changed")


def _readiness_pointer(
    client: GitHubClient,
    repository: str,
    pr: int,
    feature_head: str,
) -> dict[str, Any]:
    candidates: list[Mapping[str, Any]] = []
    for item in client.pages(f"/repos/{repository}/issues/{pr}/comments"):
        comment = _mapping(item, "pull-request issue comment")
        user = comment.get("user")
        body = comment.get("body")
        login = user.get("login") if isinstance(user, Mapping) else None
        if (
            login == "github-actions[bot]"
            and isinstance(body, str)
            and (
                body == READINESS_POINTER_MARKER
                or body.startswith(f"{READINESS_POINTER_MARKER}\n")
            )
        ):
            candidates.append(comment)
    if len(candidates) != 1:
        raise ReadinessError(
            "exactly one GitHub Actions readiness pointer comment is required"
        )
    body = candidates[0]["body"]
    if (
        not isinstance(body, str)
        or len(body.encode("utf-8")) > 4096
        or "\r" in body
        or body.count("\n") != 1
    ):
        raise ReadinessError("readiness pointer comment is malformed")
    marker, encoded = body.split("\n", 1)
    if marker != READINESS_POINTER_MARKER or not encoded:
        raise ReadinessError("readiness pointer marker is malformed")
    try:
        raw = json.loads(encoded)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReadinessError("readiness pointer payload is malformed") from exc
    pointer = _mapping(raw, "readiness pointer")
    _exact_keys(
        pointer,
        {
            "schema_version",
            "state",
            "repository",
            "pr",
            "head_sha",
            "review_digest",
            "artifact_name",
            "artifact_id",
            "artifact_digest",
            "workflow",
            "workflow_run_id",
            "workflow_run_attempt",
        },
        "readiness pointer",
    )
    if pointer["schema_version"] != 2 or pointer["state"] != "qualified":
        raise ReadinessError("readiness pointer is not qualified schema v2 evidence")
    if pointer["repository"] != repository or pointer["pr"] != pr:
        raise ReadinessError("readiness pointer repository or PR does not match")
    if pointer["head_sha"] != feature_head:
        raise ReadinessError("readiness pointer belongs to another feature head")
    review_digest = pointer["review_digest"]
    if not isinstance(review_digest, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", review_digest
    ):
        raise ReadinessError("readiness pointer review digest is malformed")
    expected_name = (
        f"pr-readiness-{pr}-{feature_head}-{review_digest.removeprefix('sha256:')}"
    )
    if pointer["artifact_name"] != expected_name:
        raise ReadinessError("readiness pointer artifact name does not match")
    artifact_digest = pointer["artifact_digest"]
    if not isinstance(artifact_digest, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", artifact_digest
    ):
        raise ReadinessError("readiness pointer artifact digest is malformed")
    if pointer["workflow"] != READINESS_WORKFLOW:
        raise ReadinessError("readiness pointer workflow does not match")
    return {
        "workflow": READINESS_WORKFLOW,
        "workflow_run_id": _positive_int(
            pointer["workflow_run_id"], "readiness pointer workflow run id"
        ),
        "workflow_run_attempt": _positive_int(
            pointer["workflow_run_attempt"],
            "readiness pointer workflow run attempt",
        ),
        "review_digest": review_digest,
        "artifact_id": _positive_int(
            pointer["artifact_id"], "readiness pointer artifact id"
        ),
        "artifact_name": expected_name,
        "artifact_digest": artifact_digest,
    }


def _verified_workflow_run(
    client: GitHubClient,
    repository: str,
    pr: int,
    feature_head: str,
    *,
    run_id: int,
    run_attempt: int,
    workflow: str,
    event: str,
    context: str,
    allow_postmerge_empty_association: bool = False,
) -> Mapping[str, Any]:
    run = _mapping(
        client.get_json(f"/repos/{repository}/actions/runs/{run_id}"), context
    )
    if run.get("id") != run_id or run.get("run_attempt") != run_attempt:
        raise ReadinessError(f"{context} identity or attempt does not match")
    if run.get("path") != workflow or run.get("event") != event:
        raise ReadinessError(f"{context} provenance does not match")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ReadinessError(f"{context} is not successfully completed")
    run_repository = run.get("repository")
    if (
        not isinstance(run_repository, Mapping)
        or run_repository.get("full_name") != repository
    ):
        raise ReadinessError(f"{context} repository does not match")
    run_head = run.get("head_repository")
    if not isinstance(run_head, Mapping) or run_head.get("full_name") != repository:
        raise ReadinessError(f"{context} head repository does not match")
    if not isinstance(run.get("head_sha"), str) or not SHA_RE.fullmatch(
        run["head_sha"]
    ):
        raise ReadinessError(f"{context} head SHA is malformed")
    associations = run.get("pull_requests")
    explicitly_omitted_postmerge = (
        allow_postmerge_empty_association
        and isinstance(associations, list)
        and not associations
    )
    if not explicitly_omitted_postmerge and (
        not isinstance(associations, list)
        or len(associations) != 1
        or not _one_exact_pr_association(
            associations, pr=pr, head_sha=feature_head, context=context
        )
    ):
        raise ReadinessError(f"{context} does not have one exact PR association")
    return run


def _newest_qualifying_workflow_run(
    client: GitHubClient,
    repository: str,
    pr: int,
    feature_head: str,
    *,
    expected_run_id: int,
    expected_run_attempt: int,
    workflow: str,
    event: str,
    context: str,
    preview: bool = False,
) -> Mapping[str, Any]:
    """Require retained evidence to remain the newest exact-head workflow run.

    A pull request can be closed and reopened without changing its head, which
    starts a distinct Source CI and Preview UI run.  The readiness invalidator
    is intentionally asynchronous, so post-merge verification must not rely on
    the old successful run while a newer same-head run is pending or failed.
    """

    workflow_file = urllib.parse.quote(PurePosixPath(workflow).name, safe="")
    payload = _mapping(
        client.get_json(
            f"/repos/{repository}/actions/workflows/{workflow_file}/runs",
            {"event": event, "head_sha": feature_head, "per_page": 100},
        ),
        f"{context} list",
    )
    runs = payload.get("workflow_runs")
    total_count = payload.get("total_count")
    if (
        not isinstance(runs, list)
        or isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count != len(runs)
    ):
        raise ReadinessError(f"{context} list is incomplete or malformed")

    qualifying: list[Mapping[str, Any]] = []
    for item in runs:
        run = _mapping(item, context)
        _positive_int(run.get("id"), f"{context} id")
        _positive_int(run.get("run_attempt"), f"{context} attempt")
        run_repository = run.get("repository")
        run_head_repository = run.get("head_repository")
        if (
            run.get("path") != workflow
            or run.get("event") != event
            or run.get("head_sha") != feature_head
            or not isinstance(run_repository, Mapping)
            or run_repository.get("full_name") != repository
            or not isinstance(run_head_repository, Mapping)
            or run_head_repository.get("full_name") != repository
        ):
            raise ReadinessError(f"{context} list provenance does not match")
        associations = run.get("pull_requests")
        if associations != [] and (
            not isinstance(associations, list)
            or len(associations) != 1
            or not _one_exact_pr_association(
                associations, pr=pr, head_sha=feature_head, context=context
            )
        ):
            raise ReadinessError(f"{context} does not have one exact PR association")
        if preview:
            kind = _preview_run_kind(run.get("display_title"), pr, feature_head)
            if kind == "ignored-label":
                continue
            if kind != "preview":
                raise ReadinessError(f"{context} has an unknown trigger")
        qualifying.append(run)

    if not qualifying:
        raise ReadinessError(f"{context} is missing")
    newest = max(qualifying, key=lambda run: run["id"])
    if (
        newest.get("id") != expected_run_id
        or newest.get("run_attempt") != expected_run_attempt
    ):
        raise ReadinessError(f"retained {context} is not the newest exact-head run")
    if newest.get("status") != "completed" or newest.get("conclusion") != "success":
        raise ReadinessError(f"newest {context} is not successfully completed")
    return newest


def _readiness_from_zip(payload: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != "readiness.json":
                raise ReadinessError("readiness archive has unexpected entries")
            member = members[0]
            mode = member.external_attr >> 16
            if (
                member.is_dir()
                or member.flag_bits & 0x1
                or member.file_size > READINESS_RECORD_LIMIT
                or member.compress_size > READINESS_ARTIFACT_LIMIT
                or stat.S_ISLNK(mode)
                or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
            ):
                raise ReadinessError("readiness archive is unsafe")
            raw = archive.read(member)
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ReadinessError("readiness artifact is not a safe ZIP") from exc
    if len(raw) > READINESS_RECORD_LIMIT:
        raise ReadinessError("readiness record exceeds its size bound")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReadinessError("readiness record is malformed") from exc
    if not isinstance(value, dict):
        raise ReadinessError("readiness record is malformed")
    return value


def _tree_contains_workflow(
    client: GitHubClient,
    repository: str,
    commit_sha: str,
) -> bool:
    commit = _mapping(
        client.get_json(f"/repos/{repository}/commits/{commit_sha}"),
        "workflow-tree commit",
    )
    if commit.get("sha") != commit_sha:
        raise ReadinessError("workflow-tree commit identity does not match")
    commit_payload = _mapping(commit.get("commit"), "workflow-tree commit payload")
    tree_ref = _mapping(commit_payload.get("tree"), "workflow-tree reference")
    tree_sha = _sha(tree_ref.get("sha"), "workflow-tree")
    tree = _mapping(
        client.get_json(
            f"/repos/{repository}/git/trees/{tree_sha}", {"recursive": "1"}
        ),
        "recursive workflow tree",
    )
    if tree.get("sha") != tree_sha or tree.get("truncated") is not False:
        raise ReadinessError("recursive workflow tree is incomplete")
    entries = tree.get("tree")
    if not isinstance(entries, list) or len(entries) > 100_000:
        raise ReadinessError("recursive workflow tree exceeds its entry bound")
    matches = []
    seen: set[str] = set()
    for item in entries:
        entry = _mapping(item, "workflow-tree entry")
        path = entry.get("path")
        entry_type = entry.get("type")
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ReadinessError("workflow-tree entry path is malformed")
        if path in seen:
            raise ReadinessError("recursive workflow tree contains duplicate paths")
        seen.add(path)
        if path == READINESS_WORKFLOW:
            if entry_type != "blob":
                raise ReadinessError("readiness workflow path is not a tracked file")
            _sha(entry.get("sha"), "readiness workflow blob")
            matches.append(entry)
    if len(matches) > 1:
        raise ReadinessError("recursive workflow tree duplicates the readiness workflow")
    return len(matches) == 1


def _verify_retained_preview(
    client: GitHubClient,
    repository: str,
    pr: int,
    feature_head: str,
    record: Mapping[str, Any],
) -> None:
    if record["preview_required"] is not True:
        if record["preview"] is not None:
            raise ReadinessError("unexpected retained preview evidence")
        return
    preview = _mapping(record["preview"], "retained preview evidence")
    run_id = preview["run_id"]
    run_attempt = preview["run_attempt"]
    _newest_qualifying_workflow_run(
        client,
        repository,
        pr,
        feature_head,
        expected_run_id=run_id,
        expected_run_attempt=run_attempt,
        workflow=PREVIEW_WORKFLOW,
        event="pull_request",
        context="preview workflow run",
        preview=True,
    )
    _verified_workflow_run(
        client,
        repository,
        pr,
        feature_head,
        run_id=run_id,
        run_attempt=run_attempt,
        workflow=PREVIEW_WORKFLOW,
        event="pull_request",
        context="retained preview workflow run",
        allow_postmerge_empty_association=True,
    )
    prefix = f"ui-preview-pr-{pr}-{feature_head}-{run_id}-{run_attempt}"
    expectations = (
        (
            "manifest",
            preview["manifest_artifact_id"],
            f"{prefix}-manifest",
            preview["manifest_artifact_digest"],
            MANIFEST_ARTIFACT_LIMIT,
        ),
        (
            "evidence",
            preview["evidence_artifact_id"],
            prefix,
            preview["evidence_artifact_digest"],
            PREVIEW_ARTIFACT_LIMIT,
        ),
    )
    for kind, artifact_id, expected_name, expected_digest, maximum in expectations:
        artifact = _mapping(
            client.get_json(f"/repos/{repository}/actions/artifacts/{artifact_id}"),
            f"retained preview {kind} artifact",
        )
        size = artifact.get("size_in_bytes")
        if (
            artifact.get("id") != artifact_id
            or artifact.get("name") != expected_name
            or artifact.get("expired") is not False
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 < size <= maximum
            or artifact.get("digest") != expected_digest
        ):
            raise ReadinessError(
                f"retained preview {kind} artifact identity does not match"
            )
        artifact_run = _mapping(
            artifact.get("workflow_run"),
            f"retained preview {kind} artifact workflow run",
        )
        if (
            artifact_run.get("id") != run_id
            or artifact_run.get("head_sha") != feature_head
        ):
            raise ReadinessError(
                f"retained preview {kind} artifact provenance does not match"
            )


def verify_published(
    client: GitHubClient,
    repository: str,
    pr: int,
    feature_head: str,
    merge_source_commit: str,
    *,
    allow_first_introduction: bool = False,
    first_introduction_pr: int | None = None,
    allowed_mergers: Sequence[str] = (),
) -> dict[str, Any]:
    """Verify an immutable, trusted feature-PR readiness lineage after merge."""

    if not isinstance(repository, str) or not REPOSITORY_RE.fullmatch(repository):
        raise ReadinessError("invalid repository identity")
    _positive_int(pr, "feature pull request number")
    _sha(feature_head, "expected feature head")
    _sha(merge_source_commit, "expected merge source commit")
    pr_path = f"/repos/{repository}/pulls/{pr}"
    if not isinstance(allow_first_introduction, bool):
        raise ReadinessError("first-introduction mode flag is malformed")
    if first_introduction_pr is not None:
        _positive_int(first_introduction_pr, "first-introduction pull request")
        if not allow_first_introduction:
            raise ReadinessError(
                "first-introduction PR requires explicit first-introduction mode"
            )
    if (
        isinstance(allowed_mergers, (str, bytes))
        or not isinstance(allowed_mergers, Sequence)
        or not all(isinstance(login, str) for login in allowed_mergers)
    ):
        raise ReadinessError("allowed merger identities are malformed")
    merger_allowlist = {
        _metadata_string(login, "allowed merger login", 100).casefold()
        for login in allowed_mergers
    }
    initial_pr = _merged_feature_pr(
        client.get_json(pr_path),
        repository=repository,
        pr=pr,
        feature_head=feature_head,
        merge_source_commit=merge_source_commit,
        context="feature pull request",
    )
    if not merger_allowlist:
        raise ReadinessError("published readiness requires an explicit merger allowlist")
    if initial_pr["merged_by"].casefold() not in merger_allowlist:
        raise ReadinessError("feature merge was performed by an unauthorized login")
    _verified_feature_commit_pr_association(
        client,
        repository,
        pr,
        feature_head,
        merge_source_commit,
        initial_pr,
    )

    commit = _mapping(
        client.get_json(f"/repos/{repository}/commits/{merge_source_commit}"),
        "merge source commit",
    )
    parents = commit.get("parents")
    if commit.get("sha") != merge_source_commit or not isinstance(parents, list):
        raise ReadinessError("merge source commit identity is malformed")
    if len(parents) != 2:
        raise ReadinessError("merge source commit is not a true two-parent merge")
    parent_shas = []
    for item in parents:
        parent = _mapping(item, "merge source commit parent")
        parent_shas.append(_sha(parent.get("sha"), "merge source commit parent"))
    if parent_shas[1] != feature_head or parent_shas[0] == parent_shas[1]:
        raise ReadinessError("merge source commit second parent is not the feature head")

    bootstrap = False
    if allow_first_introduction:
        existed_before = _tree_contains_workflow(
            client, repository, parent_shas[0]
        )
        exists_after = _tree_contains_workflow(
            client, repository, merge_source_commit
        )
        if not existed_before:
            if not exists_after:
                raise ReadinessError(
                    "first-introduction merge does not add the readiness workflow"
                )
            if first_introduction_pr != pr:
                raise ReadinessError(
                    "merge is not the explicitly configured first-introduction PR"
                )
            bootstrap = True

    if bootstrap:
        record = collect(client, repository, pr, feature_head, merged=True)
        pointer = None
    else:
        pointer = _readiness_pointer(client, repository, pr, feature_head)
        readiness_run = _verified_workflow_run(
            client,
            repository,
            pr,
            feature_head,
            run_id=pointer["workflow_run_id"],
            run_attempt=pointer["workflow_run_attempt"],
            workflow=READINESS_WORKFLOW,
            event="pull_request_target",
            context="readiness workflow run",
            allow_postmerge_empty_association=True,
        )

        artifact = _mapping(
            client.get_json(
                f"/repos/{repository}/actions/artifacts/{pointer['artifact_id']}"
            ),
            "readiness artifact",
        )
        size = artifact.get("size_in_bytes")
        if artifact.get("id") != pointer["artifact_id"]:
            raise ReadinessError("readiness artifact identity does not match")
        if artifact.get("name") != pointer["artifact_name"]:
            raise ReadinessError("readiness artifact name does not match")
        if artifact.get("expired") is not False:
            raise ReadinessError("readiness artifact is expired")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 < size <= READINESS_ARTIFACT_LIMIT
        ):
            raise ReadinessError("readiness artifact exceeds its size bound")
        if artifact.get("digest") != pointer["artifact_digest"]:
            raise ReadinessError("readiness artifact metadata digest does not match")
        artifact_run = _mapping(
            artifact.get("workflow_run"), "readiness artifact workflow run"
        )
        if (
            artifact_run.get("id") != pointer["workflow_run_id"]
            or artifact_run.get("head_sha") != readiness_run.get("head_sha")
        ):
            raise ReadinessError(
                "readiness artifact workflow provenance does not match"
            )
        payload = client.download(
            str(artifact.get("archive_download_url", "")),
            READINESS_ARTIFACT_LIMIT,
        )
        payload_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        if payload_digest != pointer["artifact_digest"]:
            raise ReadinessError("downloaded readiness artifact digest does not match")
        record = validate(_readiness_from_zip(payload))
        if (
            record["repository"] != repository
            or record["pr"] != pr
            or record["head_sha"] != feature_head
            or record["head_repository"] != repository
        ):
            raise ReadinessError("readiness record feature identity does not match")
        if record["review_evidence"]["digest"] != pointer["review_digest"]:
            raise ReadinessError("readiness pointer and record review digests differ")

    check = record["required_check"]
    _newest_qualifying_workflow_run(
        client,
        repository,
        pr,
        feature_head,
        expected_run_id=check["workflow_run_id"],
        expected_run_attempt=check["workflow_run_attempt"],
        workflow=SOURCE_WORKFLOW,
        event="pull_request",
        context="feature validation workflow run",
    )
    source_run = _verified_workflow_run(
        client,
        repository,
        pr,
        feature_head,
        run_id=check["workflow_run_id"],
        run_attempt=check["workflow_run_attempt"],
        workflow=SOURCE_WORKFLOW,
        event="pull_request",
        context="feature validation workflow run",
        allow_postmerge_empty_association=True,
    )
    if source_run.get("head_sha") != feature_head:
        raise ReadinessError("feature validation workflow belongs to another head")
    check_run = _mapping(
        client.get_json(
            f"/repos/{repository}/check-runs/{check['check_run_id']}"
        ),
        "feature validation check run",
    )
    app = check_run.get("app")
    details_url = check_run.get("details_url")
    details_pattern = re.compile(
        rf"https://github\.com/{re.escape(repository)}/actions/runs/"
        rf"{check['workflow_run_id']}/job/[1-9][0-9]*"
    )
    if (
        check_run.get("id") != check["check_run_id"]
        or check_run.get("name") != REQUIRED_CHECK
        or check_run.get("status") != "completed"
        or check_run.get("conclusion") != "success"
        or check_run.get("head_sha") != feature_head
        or not isinstance(app, Mapping)
        or app.get("slug") != "github-actions"
        or not isinstance(details_url, str)
        or not details_pattern.fullmatch(details_url)
    ):
        raise ReadinessError("feature validation check provenance does not match")

    current_reviews = _review_evidence(client, repository, pr)
    current_reviews = _validate_review_evidence(current_reviews)
    if current_reviews != record["review_evidence"]:
        raise ReadinessError("current review evidence differs from the readiness record")
    _verify_retained_preview(client, repository, pr, feature_head, record)

    final_pr = _merged_feature_pr(
        client.get_json(pr_path),
        repository=repository,
        pr=pr,
        feature_head=feature_head,
        merge_source_commit=merge_source_commit,
        context="final feature pull request",
    )
    if (
        final_pr["merged_by"] != initial_pr["merged_by"]
        or final_pr["merged_by"].casefold() not in merger_allowlist
    ):
        raise ReadinessError("feature pull-request merger identity changed")
    if final_pr["labels"] != initial_pr["labels"]:
        raise ReadinessError("feature pull-request labels changed during verification")
    current_preview_required = needs_preview(
        _changed_paths(
            client,
            repository,
            pr,
            final_pr["changed_file_count"],
        ),
        final_pr["labels"],
    )
    if current_preview_required is not record["preview_required"]:
        raise ReadinessError(
            "current pull-request labels changed preview applicability"
        )
    return {
        "schema_version": 1,
        "attestation_mode": (
            "first-introduction-postmerge" if bootstrap else "premerge-published"
        ),
        "repository": repository,
        "merger": initial_pr["merged_by"],
        "feature_pr": {"number": pr, "head_sha": feature_head},
        "merge_source_commit": merge_source_commit,
        "feature_validation": {
            "check_run_id": check["check_run_id"],
            "workflow_run_id": check["workflow_run_id"],
            "workflow_run_attempt": check["workflow_run_attempt"],
        },
        "readiness": (
            None
            if pointer is None
            else {
                "workflow_run_id": pointer["workflow_run_id"],
                "workflow_run_attempt": pointer["workflow_run_attempt"],
                "artifact_id": pointer["artifact_id"],
                "artifact_name": pointer["artifact_name"],
                "artifact_digest": pointer["artifact_digest"],
                "review_digest": pointer["review_digest"],
            }
        ),
        "review_digest": record["review_evidence"]["digest"],
        "preview_required": record["preview_required"],
        "preview": record["preview"],
    }


def _write(value: Mapping[str, Any], output: Path | None) -> None:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    if output:
        output.write_text(rendered, encoding="ascii")
    else:
        print(rendered, end="")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("evidence", type=Path)
    validate_parser.add_argument("--output", type=Path)
    github_parser = subparsers.add_parser("github")
    github_parser.add_argument("--repository", required=True)
    github_parser.add_argument("--pull-request", required=True, type=int)
    github_parser.add_argument("--event-head", required=True)
    github_parser.add_argument("--api-url", default="https://api.github.com")
    github_parser.add_argument("--output", type=Path)
    published_parser = subparsers.add_parser("published")
    published_parser.add_argument("--repository", required=True)
    published_parser.add_argument("--pull-request", required=True, type=int)
    published_parser.add_argument("--feature-head", required=True)
    published_parser.add_argument("--merge-source-commit", required=True)
    published_parser.add_argument("--allow-first-introduction", action="store_true")
    published_parser.add_argument("--first-introduction-pr", type=int)
    published_parser.add_argument("--allowed-merger", action="append", default=[])
    published_parser.add_argument("--api-url", default="https://api.github.com")
    published_parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "validate":
            result = validate(json.loads(args.evidence.read_text(encoding="utf-8")))
        elif args.command == "github":
            result = collect(
                GitHubClient(args.api_url, os.environ.get("GITHUB_TOKEN", "")),
                args.repository,
                args.pull_request,
                args.event_head,
            )
        else:
            result = verify_published(
                GitHubClient(args.api_url, os.environ.get("GITHUB_TOKEN", "")),
                args.repository,
                args.pull_request,
                args.feature_head,
                args.merge_source_commit,
                allow_first_introduction=args.allow_first_introduction,
                first_introduction_pr=args.first_introduction_pr,
                allowed_mergers=args.allowed_merger,
            )
    except (ReadinessError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"readiness not established: {exc}", file=sys.stderr)
        return 2
    _write(result, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
