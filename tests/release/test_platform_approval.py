from __future__ import annotations

import copy
import hashlib
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
ACTIVATION = "a" * 40
ACTIVATION_HEAD = "b" * 40
ACTIVATION_TREE = "c" * 40
CONTINUATION = "5" * 40
CONTINUATION_HEAD = "e" * 40
CONTINUATION_TREE = "f" * 40
RECOVERY_RUN = 666
ACTIVATION_RUN = 667
CONTINUATION_RUN = 669
REPAIR = "7" * 40
REPAIR_HEAD = "8" * 40
REPAIR_TREE = "9" * 40
REPAIR_RUN = 671
PUBLICATION = "01" * 20
PUBLICATION_HEAD = "02" * 20
PUBLICATION_TREE = "03" * 20
PUBLICATION_RUN = 673
FAILED_RECOVERY_RUN = 670
FAILED_RECOVERY_ARTIFACT_ID = 996
FAILED_RECOVERY_ARTIFACT_DIGEST = "sha256:" + "a" * 64
FAILED_RECOVERY_ARTIFACT = f"source-fast-failure-{FAILED_RECOVERY_RUN}-1"
FAILED_PUBLICATION_RUN = 672
FAILED_PUBLICATION_ARTIFACT_ID = 997
FAILED_PUBLICATION_ARTIFACT_DIGEST = "sha256:" + "b" * 64
FAILED_PUBLICATION_ARTIFACT = (
    f"platform-validation-recovery-v{VERSION}-{RELEASE[:12]}-"
    f"{FAILED_PUBLICATION_RUN}-1"
)
RECOVERY_ARTIFACT_ID = 991
RECOVERY_ARTIFACT_DIGEST = "sha256:" + "d" * 64
RECOVERY_ARTIFACT = (
    f"platform-validation-recovery-v{VERSION}-{RELEASE[:12]}-{RECOVERY_RUN}-1"
)
HISTORICAL_REQUIRED_RUN = 668
HISTORICAL_REQUIRED_CHECK = 992
RECOVERED_REQUIRED_CHECK = 993
HISTORICAL_REQUIRED_SUITE = 994
RECOVERED_REQUIRED_SUITE = 995
HISTORICAL_REQUIRED_NODE = "CR_historical_required_check"
RECOVERED_REQUIRED_NODE = "CR_recovered_required_check"
HISTORICAL_REQUIRED_COMPLETED_AT = "2026-09-09T01:09:38Z"
RECOVERED_REQUIRED_COMPLETED_AT = "2026-09-09T12:05:00Z"
SYNC_REPAIR = "04" * 20
SYNC_REPAIR_HEAD = "05" * 20
SYNC_REPAIR_TREE = "06" * 20
SYNC_HEAD = "07" * 20
SYNC_HEAD_TREE = "08" * 20
SYNC_MERGE = "09" * 20
SYNC_REPAIR_RUN = 675
SYNC_NATIVE_RUN = 676
SYNC_NATIVE_CHECK = 1001
SYNC_NATIVE_SUITE = 1002
SYNC_NATIVE_NODE = "CR_updated_sync_required_check"
SYNC_NATIVE_COMPLETED_AT = "2026-09-12T12:05:00Z"
ASSOCIATION_REPAIR = "0a" * 20
ASSOCIATION_REPAIR_HEAD = "0b" * 20
ASSOCIATION_REPAIR_TREE = "0c" * 20
ASSOCIATION_REPAIR_RUN = 677
NEXT_SYNC_HEAD = "0d" * 20
NEXT_SYNC_HEAD_TREE = "0e" * 20
NEXT_SYNC_MERGE = "0f" * 20
NEXT_SYNC_NATIVE_RUN = 678
NEXT_SYNC_NATIVE_CHECK = 1003
NEXT_SYNC_NATIVE_SUITE = 1004
NEXT_SYNC_NATIVE_NODE = "CR_check_association_sync_required_check"
NEXT_SYNC_NATIVE_COMPLETED_AT = "2026-09-13T12:05:00Z"
PREFLIGHT_EXPIRES_AT = "2099-09-16T01:07:13Z"


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


def _published_readiness(
    *,
    pull_request: int = FEATURE_PR,
    feature_head: str = FEATURE_HEAD,
    merge_source_commit: str = SOURCE,
) -> dict[str, Any]:
    readiness_name = (
        f"pr-readiness-{pull_request}-{feature_head}-"
        f"{REVIEW_DIGEST.removeprefix('sha256:')}"
    )
    return {
        "attestation_mode": "premerge-published",
        "feature_pr": {"head_sha": feature_head, "number": pull_request},
        "feature_validation": {
            "check_run_id": FEATURE_CHECK_RUN,
            "workflow_run_attempt": 1,
            "workflow_run_id": FEATURE_VALIDATION_RUN,
        },
        "merge_source_commit": merge_source_commit,
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


def _recovery_contract() -> dict[str, Any]:
    jobs = [
        {"conclusion": "success", "id": 801, "name": "Build release"},
        {"conclusion": "success", "id": 802, "name": "Preflight"},
        {"conclusion": "failure", "id": 803, "name": "Publish approval"},
    ]
    contract = {
        "activation": {
            "allowed_paths": list(approval.validation_recovery.ACTIVATION_PATHS),
            "base_commit": SOURCE,
            "commit": ACTIVATION,
            "feature_head": ACTIVATION_HEAD,
            "pull_request": 53,
            "tree": ACTIVATION_TREE,
            "validation": {"run_attempt": 1, "run_id": ACTIVATION_RUN},
        },
        "check_association_repair": {
            "allowed_paths": list(
                approval.validation_recovery.CHECK_ASSOCIATION_REPAIR_PATHS
            ),
            "base_commit": SYNC_REPAIR,
            "pull_request": 59,
        },
        "continuation": {
            "allowed_paths": list(approval.validation_recovery.CONTINUATION_PATHS),
            "base_commit": ACTIVATION,
            "commit": CONTINUATION,
            "feature_head": CONTINUATION_HEAD,
            "pull_request": 54,
            "tree": CONTINUATION_TREE,
            "validation": {"run_attempt": 1, "run_id": CONTINUATION_RUN},
        },
        "repair": {
            "allowed_paths": list(approval.validation_recovery.REPAIR_PATHS),
            "base_commit": CONTINUATION,
            "harness_paths": list(approval.validation_recovery.HARNESS_PATHS),
            "pull_request": 55,
            "test_support_paths": list(
                approval.validation_recovery.TEST_SUPPORT_PATHS
            ),
            "test_support_root": approval.validation_recovery.TEST_SUPPORT_ROOT,
        },
        "publication_repair": {
            "allowed_paths": list(
                approval.validation_recovery.PUBLICATION_REPAIR_PATHS
            ),
            "base_commit": REPAIR,
            "pull_request": 56,
        },
        "failed_publication": {
            "artifact": {
                "digest": FAILED_PUBLICATION_ARTIFACT_DIGEST,
                "id": FAILED_PUBLICATION_ARTIFACT_ID,
                "name": FAILED_PUBLICATION_ARTIFACT,
            },
            "jobs": [
                {"conclusion": "success", "id": 972, "name": "Required source checks"},
                {"conclusion": "success", "id": 973, "name": "Attest evidence"},
                {"conclusion": "failure", "id": 974, "name": "Publish readiness"},
            ],
            "run_attempt": 1,
            "run_id": FAILED_PUBLICATION_RUN,
        },
        "failed_validation": {
            "artifact": {
                "digest": FAILED_RECOVERY_ARTIFACT_DIGEST,
                "id": FAILED_RECOVERY_ARTIFACT_ID,
                "name": FAILED_RECOVERY_ARTIFACT,
            },
            "jobs": [
                {"conclusion": "failure", "id": 970, "name": "Fast source checks"},
                {
                    "conclusion": "failure",
                    "id": 971,
                    "name": "Required source checks",
                },
            ],
            "run_attempt": 1,
            "run_id": FAILED_RECOVERY_RUN,
        },
        "feature": {"head_commit": FEATURE_HEAD, "pull_request": FEATURE_PR},
        "main_validation": {"run_attempt": 1, "run_id": MAIN_RUN},
        "platform_version": VERSION,
        "preflight": {
            "artifact_digest": ARTIFACT_DIGEST,
            "artifact_id": ARTIFACT_ID,
            "artifact_name": ARTIFACT,
            "expires_at": PREFLIGHT_EXPIRES_AT,
            "failed_job": {"id": 803, "name": "Publish approval"},
            "jobs": jobs,
            "passed_job": {"id": 802, "name": "Preflight"},
            "run_attempt": 1,
            "run_id": PREFLIGHT_RUN,
        },
        "required_check": {
            "app_id": approval.validation_recovery.REQUIRED_CHECK_APP_ID,
            "app_slug": approval.validation_recovery.REQUIRED_CHECK_APP_SLUG,
            "branch": "main",
            "context": approval.validation_recovery.REQUIRED_CHECK_CONTEXT,
            "historical_failure": {
                "check_run_id": HISTORICAL_REQUIRED_CHECK,
                "conclusion": "failure",
                "details_url": (
                    f"https://github.com/{REPOSITORY}/actions/runs/"
                    f"{HISTORICAL_REQUIRED_RUN}/job/{HISTORICAL_REQUIRED_CHECK}"
                ),
                "run_attempt": 1,
                "workflow_run_id": HISTORICAL_REQUIRED_RUN,
            },
            "required_lanes": list(
                approval.validation_recovery.RECOVERY_REQUIRED_LANES
            ),
        },
        "recovery_id": approval.validation_recovery.RECOVERY_ID,
        "release": {
            "commit": RELEASE,
            "manifest_sha256": MANIFEST,
            "ref": f"release/platform-v{VERSION}",
            "source_commit": SOURCE,
            "source_tree": "d" * 40,
            "tree": "e" * 40,
        },
        "repository": REPOSITORY,
        "schema_version": approval.validation_recovery.SCHEMA_VERSION,
        "historical_readiness": {
            "approval_nonce": "0" * 64,
            "comment_id": 704,
            "created_at": "2026-09-09T12:02:00Z",
            "schema_version": approval.RECOVERY_SCHEMA_VERSION,
            "sync_base_commit": PUBLICATION,
            "sync_head_commit": RELEASE,
        },
        "sync": {
            "branch": f"sync/platform-v{VERSION}",
            "initial_head_commit": RELEASE,
            "prior_head_commit": SYNC_HEAD,
            "prior_head_tree": SYNC_HEAD_TREE,
            "pull_request": PR_NUMBER,
        },
        "sync_repair": {
            "allowed_paths": list(approval.validation_recovery.SYNC_REPAIR_PATHS),
            "base_commit": PUBLICATION,
            "pull_request": 57,
        },
    }
    attestation = _recovery_attestation()
    lanes = _recovery_lane_records()
    config = approval._config(
        owner=OWNER,
        repository=REPOSITORY,
        version=VERSION,
        source_commit=SOURCE,
        release_commit=RELEASE,
        api_url="https://api.github.com",
        server_url="https://github.com",
        output=Path("unused-recovery-output.json"),
    )
    _, binding_digest, external_id = approval._recovery_check_payload(
        config,
        contract,
        attestation,
        {
            "digest": RECOVERY_ARTIFACT_DIGEST,
            "id": RECOVERY_ARTIFACT_ID,
            "name": RECOVERY_ARTIFACT,
        },
        lanes,
    )
    contract["partial_publication"] = {
        "artifact": {
            "digest": RECOVERY_ARTIFACT_DIGEST,
            "id": RECOVERY_ARTIFACT_ID,
            "name": RECOVERY_ARTIFACT,
        },
        "attestation_sha256": "sha256:"
        + hashlib.sha256(
            approval.validation_recovery.canonical_json_bytes(attestation)
        ).hexdigest(),
        "jobs": [
            *lanes,
            {
                "conclusion": "success",
                "id": 920,
                "name": "Qualify the reviewed one-release recovery",
            },
            {
                "conclusion": "success",
                "id": 921,
                "name": "Attest separate release and harness identities",
            },
            {
                "conclusion": "failure",
                "id": 922,
                "name": "Publish the recovered one-approval boundary",
            },
        ],
        "required_check": {
            "app_id": approval.validation_recovery.REQUIRED_CHECK_APP_ID,
            "app_slug": approval.validation_recovery.REQUIRED_CHECK_APP_SLUG,
            "binding_digest": binding_digest,
            "check_run_node_id": RECOVERED_REQUIRED_NODE,
            "check_run_id": RECOVERED_REQUIRED_CHECK,
            "check_suite_id": RECOVERED_REQUIRED_SUITE,
            "completed_at": RECOVERED_REQUIRED_COMPLETED_AT,
            "context": approval.validation_recovery.REQUIRED_CHECK_CONTEXT,
            "details_url": f"https://github.com/{REPOSITORY}/runs/{RECOVERED_REQUIRED_CHECK}",
            "external_id": external_id,
            "head_sha": RELEASE,
            "historical_check_run_id": HISTORICAL_REQUIRED_CHECK,
            "required_pull_request": PR_NUMBER,
        },
        "run_attempt": 1,
        "run_id": RECOVERY_RUN,
    }
    return contract


def _current_recovery_contract(
    historical: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fixture whose historical identity binds the real v1 payload."""

    contract = _recovery_contract()
    historical = historical or _recovery_ready_payload()
    contract["historical_readiness"] = {
        "approval_nonce": historical["approval_nonce"],
        "comment_id": 704,
        "created_at": "2026-09-09T12:02:00Z",
        "schema_version": approval.RECOVERY_SCHEMA_VERSION,
        "sync_base_commit": PUBLICATION,
        "sync_head_commit": RELEASE,
    }
    return contract


def _sync_repair_report() -> dict[str, Any]:
    return {
        "base_commit": PUBLICATION,
        "feature_head": SYNC_REPAIR_HEAD,
        "paths": [
            {
                "git_blob_sha": f"{index + 701:040x}",
                "path": path,
                "sha256": f"{index + 701:064x}",
            }
            for index, path in enumerate(
                approval.validation_recovery.SYNC_REPAIR_PATHS
            )
        ],
        "pull_request": 57,
        "sync_repair_commit": SYNC_REPAIR,
        "sync_repair_tree": SYNC_REPAIR_TREE,
    }


def _sync_update_report() -> dict[str, Any]:
    return {
        "base_commit": ASSOCIATION_REPAIR,
        "check_association_repair": _check_association_repair_report(),
        "initial_head_commit": RELEASE,
        "previous_head_commit": SYNC_HEAD,
        "pull_request": PR_NUMBER,
        "release_commit": RELEASE,
        "sync_branch": f"sync/platform-v{VERSION}",
        "sync_head_commit": NEXT_SYNC_HEAD,
        "sync_head_tree": NEXT_SYNC_HEAD_TREE,
        "sync_repair": _sync_repair_report(),
    }


def _check_association_repair_report() -> dict[str, Any]:
    return {
        "base_commit": SYNC_REPAIR,
        "check_association_repair_commit": ASSOCIATION_REPAIR,
        "check_association_repair_tree": ASSOCIATION_REPAIR_TREE,
        "feature_head": ASSOCIATION_REPAIR_HEAD,
        "paths": [
            {
                "git_blob_sha": f"{index + 801:040x}",
                "path": path,
                "sha256": f"{index + 801:064x}",
            }
            for index, path in enumerate(
                approval.validation_recovery.CHECK_ASSOCIATION_REPAIR_PATHS
            )
        ],
        "pull_request": 59,
        "sync_repair": _sync_repair_report(),
    }


def _recovery_attestation() -> dict[str, Any]:
    paths = [
        {
            "git_blob_sha": f"{index + 1:040x}",
            "path": path,
            "sha256": f"{index + 1:064x}",
        }
        for index, path in enumerate(approval.validation_recovery.ACTIVATION_PATHS)
    ]
    by_path = {item["path"]: item for item in paths}
    continuation_paths = [
        {
            "git_blob_sha": f"{index + 101:040x}",
            "path": path,
            "sha256": f"{index + 101:064x}",
        }
        for index, path in enumerate(
            approval.validation_recovery.CONTINUATION_PATHS
        )
    ]
    by_path.update({item["path"]: item for item in continuation_paths})
    repair_paths = [
        {
            "git_blob_sha": f"{index + 301:040x}",
            "path": path,
            "sha256": f"{index + 301:064x}",
        }
        for index, path in enumerate(approval.validation_recovery.REPAIR_PATHS)
    ]
    by_path.update({item["path"]: item for item in repair_paths})
    support_files = [
        {
            "git_blob_sha": f"{index + 201:040x}",
            "path": path,
            "sha256": f"{index + 201:064x}",
            "staged_path": (
                f"{approval.validation_recovery.TEST_SUPPORT_ROOT}/{path}"
            ),
        }
        for index, path in enumerate(approval.validation_recovery.TEST_SUPPORT_PATHS)
    ]
    return {
        "activation": {
            "activation_commit": ACTIVATION,
            "activation_tree": ACTIVATION_TREE,
            "base_commit": SOURCE,
            "feature_head": ACTIVATION_HEAD,
            "paths": paths,
            "pull_request": 53,
        },
        "continuation": {
            "base_commit": ACTIVATION,
            "continuation_commit": CONTINUATION,
            "continuation_tree": CONTINUATION_TREE,
            "feature_head": CONTINUATION_HEAD,
            "paths": continuation_paths,
            "pull_request": 54,
        },
        "harness": {
            "commit": REPAIR,
            "files": [
                copy.deepcopy(by_path[path])
                for path in approval.validation_recovery.HARNESS_PATHS
            ],
            "test_support": {
                "files": support_files,
                "root": approval.validation_recovery.TEST_SUPPORT_ROOT,
            },
            "tree": REPAIR_TREE,
        },
        "recovery_id": approval.validation_recovery.RECOVERY_ID,
        "release": {
            "commit": RELEASE,
            "manifest_sha256": MANIFEST,
            "ref": f"release/platform-v{VERSION}",
            "source_commit": SOURCE,
            "source_tree": "d" * 40,
            "tree": "e" * 40,
        },
        "repository": REPOSITORY,
        "repair": {
            "base_commit": CONTINUATION,
            "feature_head": REPAIR_HEAD,
            "paths": repair_paths,
            "pull_request": 55,
            "repair_commit": REPAIR,
            "repair_tree": REPAIR_TREE,
        },
        "schema_version": approval.validation_recovery.ATTESTATION_SCHEMA_VERSION,
        "validation": {
            "actor": OWNER,
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": REPAIR,
            "result": "success",
            "run_attempt": 1,
            "run_id": RECOVERY_RUN,
            "workflow_path": approval.validation_recovery.WORKFLOW_PATH,
        },
    }


def _publication_repair_evidence() -> dict[str, Any]:
    return {
        "base_commit": REPAIR,
        "feature_head": PUBLICATION_HEAD,
        "paths": [
            {
                "git_blob_sha": f"{index + 501:040x}",
                "path": path,
                "sha256": f"{index + 501:064x}",
            }
            for index, path in enumerate(
                approval.validation_recovery.PUBLICATION_REPAIR_PATHS
            )
        ],
        "publication_repair_commit": PUBLICATION,
        "publication_repair_tree": PUBLICATION_TREE,
        "pull_request": 56,
    }


def _recovery_ready_payload() -> dict[str, Any]:
    contract = _recovery_contract()
    attestation = _recovery_attestation()
    lanes = _recovery_lane_records()
    value = {
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
        "recovery_validation": {
            "activation_readiness": _published_readiness(
                pull_request=53,
                feature_head=ACTIVATION_HEAD,
                merge_source_commit=ACTIVATION,
            ),
            "activation_validation_run_attempt": 1,
            "activation_validation_run_id": ACTIVATION_RUN,
            "artifact_digest": RECOVERY_ARTIFACT_DIGEST,
            "artifact_id": RECOVERY_ARTIFACT_ID,
            "artifact_name": RECOVERY_ARTIFACT,
            "attestation": attestation,
            "continuation_readiness": _published_readiness(
                pull_request=54,
                feature_head=CONTINUATION_HEAD,
                merge_source_commit=CONTINUATION,
            ),
            "continuation_validation_run_attempt": 1,
            "continuation_validation_run_id": CONTINUATION_RUN,
            "failed_publication": {
                "artifact_digest": FAILED_PUBLICATION_ARTIFACT_DIGEST,
                "artifact_id": FAILED_PUBLICATION_ARTIFACT_ID,
                "artifact_name": FAILED_PUBLICATION_ARTIFACT,
                "run_attempt": 1,
                "run_id": FAILED_PUBLICATION_RUN,
            },
            "failed_validation": {
                "artifact_digest": FAILED_RECOVERY_ARTIFACT_DIGEST,
                "artifact_id": FAILED_RECOVERY_ARTIFACT_ID,
                "artifact_name": FAILED_RECOVERY_ARTIFACT,
                "run_attempt": 1,
                "run_id": FAILED_RECOVERY_RUN,
            },
            "mode": "published-release-recovery",
            "partial_publication": {
                "artifact_digest": RECOVERY_ARTIFACT_DIGEST,
                "artifact_id": RECOVERY_ARTIFACT_ID,
                "artifact_name": RECOVERY_ARTIFACT,
                "failed_job_id": 922,
                "run_attempt": 1,
                "run_id": RECOVERY_RUN,
            },
            "publication_repair": _publication_repair_evidence(),
            "publication_repair_readiness": _published_readiness(
                pull_request=56,
                feature_head=PUBLICATION_HEAD,
                merge_source_commit=PUBLICATION,
            ),
            "publication_repair_validation_run_attempt": 1,
            "publication_repair_validation_run_id": PUBLICATION_RUN,
            "repair_readiness": _published_readiness(
                pull_request=55,
                feature_head=REPAIR_HEAD,
                merge_source_commit=REPAIR,
            ),
            "repair_validation_run_attempt": 1,
            "repair_validation_run_id": REPAIR_RUN,
            "required_check": {
                **contract["partial_publication"]["required_check"],
                "lanes": lanes,
            },
        },
        "release_commit": RELEASE,
        "repository": REPOSITORY,
        "schema_version": approval.RECOVERY_SCHEMA_VERSION,
        "source_commit": SOURCE,
        "sync_validation_run_attempt": 1,
        "sync_validation_run_id": RECOVERY_RUN,
    }
    self_check = approval.validation_recovery.validate_attestation_data(
        value["recovery_validation"]["attestation"], contract=contract
    )
    if self_check != value["recovery_validation"]["attestation"]:
        raise AssertionError("recovery fixture normalization changed")
    value["approval_nonce"] = approval._nonce(value)
    return value


def _recovery_lane_records() -> list[dict[str, Any]]:
    return [
        {
            "conclusion": "success",
            "id": 900 + index,
            "name": f"{approval.RECOVERY_SOURCE_JOB_PREFIX} / {lane}",
        }
        for index, lane in enumerate(
            approval.validation_recovery.RECOVERY_REQUIRED_LANES, start=1
        )
    ]


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


def _recovery_event() -> dict[str, Any]:
    event = _event()
    event["pull_request"]["merged_at"] = "2026-09-11T19:00:00Z"
    return event


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
                            "expires_at": PREFLIGHT_EXPIRES_AT,
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
        if suffix == "actions/artifacts/777" and method == "GET":
            self.calls.append((method, parsed.path))
            return _response({"id": 777, "digest": READINESS_DIGEST, "expired": False, "expires_at": PREFLIGHT_EXPIRES_AT})
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


class RecoveryTransport(ApprovalTransport):
    def __init__(
        self,
        *,
        ready: bool,
        payload: dict[str, Any] | None = None,
        include_merge_marker: bool = True,
    ) -> None:
        super().__init__(include_merge_marker=include_merge_marker)
        self.ready_mode = ready
        self.recovery_payload = payload or _recovery_ready_payload()
        self.main_tip = PUBLICATION if ready else MERGE
        self.recovery_run_path = approval.validation_recovery.WORKFLOW_PATH
        self.recovery_workflow_title = (
            f"Validate published v0.6.2 / {REPAIR} / {OWNER}"
        )
        self.recovery_run_status = "completed"
        self.recovery_run_conclusion = "failure"
        self.activation_detail_status = 200
        self.activation_detail = self._activation_pr()
        self.continuation_detail_status = 200
        self.continuation_detail = self._continuation_pr()
        self.repair_detail_status = 200
        self.repair_detail = self._repair_pr()
        self.publication_detail_status = 200
        self.publication_detail = self._publication_pr()
        self.deny_check_publication = False
        self.published_check_payload: dict[str, Any] | None = None
        self.required_check_published = True
        self.latest_required_check_id: int | None = None
        self.post_publication_required_checks: list[dict[str, Any]] = []
        self.non_required_check_nodes: set[str] = set()
        self.graphql_missing_reads = 0
        self.graphql_node_overrides: dict[str, Any] = {}
        self.graphql_error_payload: dict[str, Any] | None = None
        self.recovered_check_overrides: dict[str, Any] = {}
        self.check_association_head = RELEASE
        self.check_association_base = PUBLICATION
        self.branch_checks = [
            {
                "app_id": approval.validation_recovery.REQUIRED_CHECK_APP_ID,
                "context": approval.validation_recovery.REQUIRED_CHECK_CONTEXT,
            }
        ]
        self.branch_contexts = [
            approval.validation_recovery.REQUIRED_CHECK_CONTEXT
        ]
        self.activation_run = {
            "conclusion": "success",
            "event": "push",
            "head_branch": "main",
            "head_repository": {"full_name": REPOSITORY},
            "head_sha": ACTIVATION,
            "id": ACTIVATION_RUN,
            "path": ".github/workflows/source-ci.yml@refs/heads/main",
            "repository": {"full_name": REPOSITORY},
            "run_attempt": 1,
            "run_number": 80,
            "status": "completed",
        }
        self.continuation_run = {
            "conclusion": "success",
            "event": "push",
            "head_branch": "main",
            "head_repository": {"full_name": REPOSITORY},
            "head_sha": CONTINUATION,
            "id": CONTINUATION_RUN,
            "path": ".github/workflows/source-ci.yml@refs/heads/main",
            "repository": {"full_name": REPOSITORY},
            "run_attempt": 1,
            "run_number": 82,
            "status": "completed",
        }
        self.repair_run = {
            "conclusion": "success",
            "event": "push",
            "head_branch": "main",
            "head_repository": {"full_name": REPOSITORY},
            "head_sha": REPAIR,
            "id": REPAIR_RUN,
            "path": ".github/workflows/source-ci.yml@refs/heads/main",
            "repository": {"full_name": REPOSITORY},
            "run_attempt": 1,
            "run_number": 84,
            "status": "completed",
        }
        self.publication_run = {
            "conclusion": "success",
            "event": "push",
            "head_branch": "main",
            "head_repository": {"full_name": REPOSITORY},
            "head_sha": PUBLICATION,
            "id": PUBLICATION_RUN,
            "path": ".github/workflows/source-ci.yml@refs/heads/main",
            "repository": {"full_name": REPOSITORY},
            "run_attempt": 1,
            "run_number": 86,
            "status": "completed",
        }

    def _recovery_run(self) -> dict[str, Any]:
        return {
            "actor": {"login": OWNER},
            "conclusion": self.recovery_run_conclusion,
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_repository": {"full_name": REPOSITORY},
            "head_sha": REPAIR,
            "id": RECOVERY_RUN,
            "path": f"{self.recovery_run_path}@refs/heads/main",
            "repository": {"full_name": REPOSITORY},
            "run_attempt": 1,
            "run_number": 81,
            "status": self.recovery_run_status,
            "triggering_actor": {"login": OWNER},
        }

    @staticmethod
    def _activation_pr() -> dict[str, Any]:
        return {
            "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
            "draft": False,
            "head": {
                "ref": "codex/recovery",
                "repo": {"full_name": REPOSITORY},
                "sha": ACTIVATION_HEAD,
            },
            "merge_commit_sha": ACTIVATION,
            "merged_at": "2026-09-09T12:00:00Z",
            "merged_by": {"login": OWNER},
            "number": 53,
            "state": "closed",
            "user": {"login": OWNER},
        }

    @staticmethod
    def _continuation_pr() -> dict[str, Any]:
        return {
            "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
            "draft": False,
            "head": {
                "ref": "codex/recovery-continuation",
                "repo": {"full_name": REPOSITORY},
                "sha": CONTINUATION_HEAD,
            },
            "merge_commit_sha": CONTINUATION,
            "merged_at": "2026-09-10T12:00:00Z",
            "merged_by": {"login": OWNER},
            "number": 54,
            "state": "closed",
            "user": {"login": OWNER},
        }

    @staticmethod
    def _repair_pr() -> dict[str, Any]:
        return {
            "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
            "draft": False,
            "head": {
                "ref": "codex/recovery-artifact-digest",
                "repo": {"full_name": REPOSITORY},
                "sha": REPAIR_HEAD,
            },
            "merge_commit_sha": REPAIR,
            "merged_at": "2026-09-11T12:00:00Z",
            "merged_by": {"login": OWNER},
            "number": 55,
            "state": "closed",
            "user": {"login": OWNER},
        }

    @staticmethod
    def _publication_pr() -> dict[str, Any]:
        return {
            "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
            "draft": False,
            "head": {
                "ref": "codex/recovery-check-publication",
                "repo": {"full_name": REPOSITORY},
                "sha": PUBLICATION_HEAD,
            },
            "merge_commit_sha": PUBLICATION,
            "merged_at": "2026-09-11T18:00:00Z",
            "merged_by": {"login": OWNER},
            "number": 56,
            "state": "closed",
            "user": {"login": OWNER},
        }

    @classmethod
    def _activation_association(cls) -> dict[str, Any]:
        # Actual commit-to-PR responses omit the merger field. The verifier
        # must fetch full PR detail before deciding who merged it.
        value = cls._activation_pr()
        value.pop("merged_by")
        return value

    @classmethod
    def _continuation_association(cls) -> dict[str, Any]:
        value = cls._continuation_pr()
        value.pop("merged_by")
        return value

    @classmethod
    def _repair_association(cls) -> dict[str, Any]:
        value = cls._repair_pr()
        value.pop("merged_by")
        return value

    @classmethod
    def _publication_association(cls) -> dict[str, Any]:
        value = cls._publication_pr()
        value.pop("merged_by")
        return value

    @staticmethod
    def _historical_required_run() -> dict[str, Any]:
        return {
            "actor": {"login": OWNER},
            "conclusion": "failure",
            "event": "pull_request",
            "head_branch": f"sync/platform-v{VERSION}",
            "head_repository": {"full_name": REPOSITORY},
            "head_sha": RELEASE,
            "id": HISTORICAL_REQUIRED_RUN,
            "path": ".github/workflows/source-ci.yml",
            "pull_requests": [{"number": PR_NUMBER}],
            "repository": {"full_name": REPOSITORY},
            "run_attempt": 1,
            "status": "completed",
            "triggering_actor": {"login": OWNER},
        }

    def _required_check_association(self) -> list[dict[str, Any]]:
        repository = {
            "id": 1351004826,
            "name": REPOSITORY.split("/", 1)[1],
            "url": f"https://api.github.com/repos/{REPOSITORY}",
        }
        return [
            {
                "base": {
                    "ref": "main",
                    "repo": repository,
                    "sha": self.check_association_base,
                },
                "head": {
                    "ref": f"sync/platform-v{VERSION}",
                    "repo": repository,
                    "sha": self.check_association_head,
                },
                "number": PR_NUMBER,
            }
        ]

    def _required_check(self, *, historical: bool) -> dict[str, Any]:
        if historical:
            return {
                "app": {
                    "id": approval.validation_recovery.REQUIRED_CHECK_APP_ID,
                    "slug": approval.validation_recovery.REQUIRED_CHECK_APP_SLUG,
                },
                "conclusion": "failure",
                "completed_at": HISTORICAL_REQUIRED_COMPLETED_AT,
                "details_url": (
                    f"https://github.com/{REPOSITORY}/actions/runs/"
                    f"{HISTORICAL_REQUIRED_RUN}/job/{HISTORICAL_REQUIRED_CHECK}"
                ),
                "external_id": "historical-source-ci-job",
                "head_sha": RELEASE,
                "id": HISTORICAL_REQUIRED_CHECK,
                "name": approval.validation_recovery.REQUIRED_CHECK_CONTEXT,
                "node_id": HISTORICAL_REQUIRED_NODE,
                "check_suite": {"id": HISTORICAL_REQUIRED_SUITE},
                "pull_requests": self._required_check_association(),
                "started_at": "2026-09-09T01:09:34Z",
                "status": "completed",
            }
        payload = self.published_check_payload
        if payload is None:
            expected = _recovery_ready_payload()["recovery_validation"][
                "required_check"
            ]
            config = approval._config(
                owner=OWNER,
                repository=REPOSITORY,
                version=VERSION,
                source_commit=SOURCE,
                release_commit=RELEASE,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path("unused-recovery-output.json"),
            )
            payload, _, _ = approval._recovery_check_payload(
                config,
                _recovery_contract(),
                _recovery_attestation(),
                {
                    "digest": RECOVERY_ARTIFACT_DIGEST,
                    "id": RECOVERY_ARTIFACT_ID,
                    "name": RECOVERY_ARTIFACT,
                },
                expected["lanes"],
            )
        value = {
            **copy.deepcopy(payload),
            "app": {
                "id": approval.validation_recovery.REQUIRED_CHECK_APP_ID,
                "slug": approval.validation_recovery.REQUIRED_CHECK_APP_SLUG,
            },
            "check_suite": {"id": RECOVERED_REQUIRED_SUITE},
            "completed_at": RECOVERED_REQUIRED_COMPLETED_AT,
            "details_url": (
                f"https://github.com/{REPOSITORY}/runs/"
                f"{RECOVERED_REQUIRED_CHECK}"
            ),
            "id": RECOVERED_REQUIRED_CHECK,
            "node_id": RECOVERED_REQUIRED_NODE,
            "pull_requests": self._required_check_association(),
            "started_at": "2026-09-09T12:04:59Z",
        }
        value.update(copy.deepcopy(self.recovered_check_overrides))
        return value

    def _recovery_jobs(self) -> list[dict[str, Any]]:
        jobs = [
            {
                **item,
                "head_sha": REPAIR,
                "run_attempt": 1,
                "run_id": RECOVERY_RUN,
                "status": "completed",
                "workflow_name": self.recovery_workflow_title,
            }
            for item in _recovery_lane_records()
        ]
        jobs.extend(
            [
                {
                    "conclusion": "success",
                    "head_sha": REPAIR,
                    "id": 920,
                    "name": "Qualify the reviewed one-release recovery",
                    "run_attempt": 1,
                    "run_id": RECOVERY_RUN,
                    "status": "completed",
                    "workflow_name": self.recovery_workflow_title,
                },
                {
                    "conclusion": "success",
                    "head_sha": REPAIR,
                    "id": 921,
                    "name": "Attest separate release and harness identities",
                    "run_attempt": 1,
                    "run_id": RECOVERY_RUN,
                    "status": "completed",
                    "workflow_name": self.recovery_workflow_title,
                },
                {
                    "conclusion": "failure",
                    "head_sha": REPAIR,
                    "id": 922,
                    "name": "Publish the recovered one-approval boundary",
                    "run_attempt": 1,
                    "run_id": RECOVERY_RUN,
                    "status": "completed",
                    "workflow_name": self.recovery_workflow_title,
                },
            ]
        )
        return jobs

    def request(self, method, url, headers, body, timeout):
        del headers, timeout
        parsed = urllib.parse.urlsplit(url)
        suffix = parsed.path.removeprefix(f"/repos/{REPOSITORY}/")
        self.calls.append((method, parsed.path))
        if parsed.path == "/graphql":
            if method != "POST" or body is None:
                raise AssertionError("unexpected GraphQL request")
            request = json.loads(body)
            if "query RequiredRecoveryCheck" not in request.get("query", ""):
                raise AssertionError("unexpected GraphQL operation")
            variables = request.get("variables")
            if (
                not isinstance(variables, dict)
                or set(variables)
                != {"checkRunId", "owner", "repository", "pullRequest"}
                or variables.get("owner") != OWNER
                or variables.get("repository") != REPOSITORY.split("/", 1)[1]
                or variables.get("pullRequest") != PR_NUMBER
            ):
                raise AssertionError(f"unexpected GraphQL variables: {variables}")
            node_id = variables["checkRunId"]
            if node_id == HISTORICAL_REQUIRED_NODE:
                check = self._required_check(historical=True)
            elif node_id == RECOVERED_REQUIRED_NODE:
                check = self._required_check(historical=False)
                if self.graphql_error_payload is not None:
                    return _response(copy.deepcopy(self.graphql_error_payload))
                if self.graphql_missing_reads:
                    self.graphql_missing_reads -= 1
                    return _response(
                        {
                            "data": {
                                "node": None,
                                "repository": {
                                    "nameWithOwner": REPOSITORY,
                                    "pullRequest": {
                                        "headRefOid": self.check_association_head,
                                        "number": PR_NUMBER,
                                        "state": (
                                            "OPEN" if self.ready_mode else "MERGED"
                                        ),
                                    },
                                },
                            }
                        }
                    )
            else:
                raise AssertionError(f"unexpected check node: {node_id}")
            node = {
                "__typename": "CheckRun",
                "checkSuite": {
                    "commit": {"oid": RELEASE},
                    "databaseId": check["check_suite"]["id"],
                },
                "conclusion": check["conclusion"].upper(),
                "databaseId": check["id"],
                "detailsUrl": check["details_url"],
                "externalId": check["external_id"],
                "isRequired": node_id not in self.non_required_check_nodes,
                "name": check["name"],
                "repository": {"nameWithOwner": REPOSITORY},
                "status": check["status"].upper(),
            }
            if node_id == RECOVERED_REQUIRED_NODE:
                node.update(copy.deepcopy(self.graphql_node_overrides))
            return _response(
                {
                    "data": {
                        "node": node,
                        "repository": {
                            "nameWithOwner": REPOSITORY,
                            "pullRequest": {
                                "headRefOid": self.check_association_head,
                                "number": PR_NUMBER,
                                "state": "OPEN" if self.ready_mode else "MERGED",
                            },
                        },
                    }
                }
            )
        if suffix == f"pulls/{PR_NUMBER}" and method == "GET":
            pr = copy.deepcopy(_event()["pull_request"])
            pr["state"] = "open" if self.ready_mode else "closed"
            pr["head"]["sha"] = self.check_association_head
            pr["base"]["sha"] = self.check_association_base
            return _response(pr)
        if suffix == f"git/ref/heads/sync/platform-v{VERSION}":
            return _response(
                {
                    "ref": f"refs/heads/sync/platform-v{VERSION}",
                    "object": {"sha": RELEASE, "type": "commit"},
                }
            )
        if suffix == f"git/ref/heads/release/platform-v{VERSION}":
            return _response(
                {
                    "ref": f"refs/heads/release/platform-v{VERSION}",
                    "object": {"sha": RELEASE, "type": "commit"},
                }
            )
        if suffix == "git/ref/heads/main":
            return _response(
                {
                    "ref": "refs/heads/main",
                    "object": {"sha": self.main_tip, "type": "commit"},
                }
            )
        if suffix == f"commits/{RELEASE}":
            return _response({"sha": RELEASE, "parents": [{"sha": SOURCE}]})
        if suffix == f"commits/{ACTIVATION}":
            return _response(
                {
                    "commit": {"tree": {"sha": ACTIVATION_TREE}},
                    "parents": [{"sha": SOURCE}, {"sha": ACTIVATION_HEAD}],
                    "sha": ACTIVATION,
                }
            )
        if suffix == f"commits/{ACTIVATION_HEAD}":
            return _response(
                {
                    "commit": {"tree": {"sha": ACTIVATION_TREE}},
                    "parents": [{"sha": SOURCE}],
                    "sha": ACTIVATION_HEAD,
                }
            )
        if suffix == f"commits/{CONTINUATION}":
            return _response(
                {
                    "commit": {"tree": {"sha": CONTINUATION_TREE}},
                    "parents": [{"sha": ACTIVATION}, {"sha": CONTINUATION_HEAD}],
                    "sha": CONTINUATION,
                }
            )
        if suffix == f"commits/{CONTINUATION_HEAD}":
            return _response(
                {
                    "commit": {"tree": {"sha": CONTINUATION_TREE}},
                    "parents": [{"sha": ACTIVATION}],
                    "sha": CONTINUATION_HEAD,
                }
            )
        if suffix == f"commits/{REPAIR}":
            return _response(
                {
                    "commit": {"tree": {"sha": REPAIR_TREE}},
                    "parents": [{"sha": CONTINUATION}, {"sha": REPAIR_HEAD}],
                    "sha": REPAIR,
                }
            )
        if suffix == f"commits/{REPAIR_HEAD}":
            return _response(
                {
                    "commit": {"tree": {"sha": REPAIR_TREE}},
                    "parents": [{"sha": CONTINUATION}],
                    "sha": REPAIR_HEAD,
                }
            )
        if suffix == f"commits/{PUBLICATION}":
            return _response(
                {
                    "commit": {"tree": {"sha": PUBLICATION_TREE}},
                    "parents": [{"sha": REPAIR}, {"sha": PUBLICATION_HEAD}],
                    "sha": PUBLICATION,
                }
            )
        if suffix == f"commits/{PUBLICATION_HEAD}":
            return _response(
                {
                    "commit": {"tree": {"sha": PUBLICATION_TREE}},
                    "parents": [{"sha": REPAIR}],
                    "sha": PUBLICATION_HEAD,
                }
            )
        if suffix == f"commits/{ACTIVATION}/pulls":
            return _response([self._activation_association()])
        if suffix == "pulls/53":
            if self.activation_detail_status != 200:
                return sync.HttpResponse(
                    self.activation_detail_status,
                    b'{"message":"private activation detail"}\n',
                )
            return _response(self.activation_detail)
        if suffix == f"commits/{CONTINUATION}/pulls":
            return _response([self._continuation_association()])
        if suffix == "pulls/54":
            if self.continuation_detail_status != 200:
                return sync.HttpResponse(
                    self.continuation_detail_status,
                    b'{"message":"private continuation detail"}\n',
                )
            return _response(self.continuation_detail)
        if suffix == f"commits/{REPAIR}/pulls":
            return _response([self._repair_association()])
        if suffix == "pulls/55":
            if self.repair_detail_status != 200:
                return sync.HttpResponse(
                    self.repair_detail_status,
                    b'{"message":"private digest repair detail"}\n',
                )
            return _response(self.repair_detail)
        if suffix == f"commits/{PUBLICATION}/pulls":
            return _response([self._publication_association()])
        if suffix == "pulls/56":
            if self.publication_detail_status != 200:
                return sync.HttpResponse(
                    self.publication_detail_status,
                    b'{"message":"private publication detail"}\n',
                )
            return _response(self.publication_detail)
        if suffix == f"commits/{MERGE}":
            message = f"Sync platform release v{VERSION}"
            if self.include_merge_marker:
                message += "\n\n" + approval.marker(
                    approval.RECOVERY_APPROVAL_PREFIX,
                    approval.approval_payload(self.recovery_payload),
                )
            return _response(
                {
                    "commit": {"message": message},
                    "parents": [{"sha": PUBLICATION}, {"sha": RELEASE}],
                    "sha": MERGE,
                }
            )
        if suffix == f"actions/runs/{MAIN_RUN}":
            return _response(_run(kind="main"))
        if suffix == f"actions/runs/{ACTIVATION_RUN}":
            return _response(self.activation_run)
        if suffix == f"actions/runs/{CONTINUATION_RUN}":
            return _response(self.continuation_run)
        if suffix == f"actions/runs/{REPAIR_RUN}":
            return _response(self.repair_run)
        if suffix == f"actions/runs/{PUBLICATION_RUN}":
            return _response(self.publication_run)
        if suffix == f"actions/runs/{FAILED_RECOVERY_RUN}":
            return _response(
                {
                    "actor": {"login": OWNER},
                    "conclusion": "failure",
                    "event": "workflow_dispatch",
                    "head_branch": "main",
                    "head_repository": {"full_name": REPOSITORY},
                    "head_sha": ACTIVATION,
                    "id": FAILED_RECOVERY_RUN,
                    "path": (
                        f"{approval.validation_recovery.WORKFLOW_PATH}@refs/heads/main"
                    ),
                    "repository": {"full_name": REPOSITORY},
                    "run_attempt": 1,
                    "status": "completed",
                    "triggering_actor": {"login": OWNER},
                }
            )
        if suffix == f"actions/runs/{FAILED_RECOVERY_RUN}/jobs":
            jobs = [
                {
                    **item,
                    "head_sha": ACTIVATION,
                    "run_attempt": 1,
                    "run_id": FAILED_RECOVERY_RUN,
                    "status": "completed",
                    "workflow_name": (
                        f"Validate published v0.6.2 / {ACTIVATION} / {OWNER}"
                    ),
                }
                for item in _recovery_contract()["failed_validation"]["jobs"]
            ]
            return _response({"jobs": jobs, "total_count": len(jobs)})
        if suffix == f"actions/runs/{FAILED_PUBLICATION_RUN}":
            return _response(
                {
                    "actor": {"login": OWNER},
                    "conclusion": "failure",
                    "event": "workflow_dispatch",
                    "head_branch": "main",
                    "head_repository": {"full_name": REPOSITORY},
                    "head_sha": CONTINUATION,
                    "id": FAILED_PUBLICATION_RUN,
                    "path": (
                        f"{approval.validation_recovery.WORKFLOW_PATH}@refs/heads/main"
                    ),
                    "repository": {"full_name": REPOSITORY},
                    "run_attempt": 1,
                    "status": "completed",
                    "triggering_actor": {"login": OWNER},
                }
            )
        if suffix == f"actions/runs/{FAILED_PUBLICATION_RUN}/jobs":
            jobs = [
                {
                    **item,
                    "head_sha": CONTINUATION,
                    "run_attempt": 1,
                    "run_id": FAILED_PUBLICATION_RUN,
                    "status": "completed",
                    "workflow_name": (
                        f"Validate published v0.6.2 / {CONTINUATION} / {OWNER}"
                    ),
                }
                for item in _recovery_contract()["failed_publication"]["jobs"]
            ]
            return _response({"jobs": jobs, "total_count": len(jobs)})
        if suffix == f"actions/runs/{PREFLIGHT_RUN}":
            run = _run(kind="preflight")
            run.update(
                {
                    "actor": {"login": OWNER},
                    "conclusion": "failure",
                    "status": "completed",
                    "triggering_actor": {"login": OWNER},
                }
            )
            return _response(run)
        if suffix == f"actions/runs/{PREFLIGHT_RUN}/jobs":
            jobs = [
                {
                    **item,
                    "head_sha": SOURCE,
                    "run_attempt": 1,
                    "run_id": PREFLIGHT_RUN,
                    "status": "completed",
                    "workflow_name": "Prepare Release",
                }
                for item in _recovery_contract()["preflight"]["jobs"]
            ]
            return _response({"jobs": jobs, "total_count": len(jobs)})
        if suffix == f"actions/runs/{RECOVERY_RUN}":
            return _response(self._recovery_run())
        if suffix == f"actions/runs/{RECOVERY_RUN}/jobs":
            jobs = self._recovery_jobs()
            return _response({"jobs": jobs, "total_count": len(jobs)})
        if suffix == f"actions/runs/{HISTORICAL_REQUIRED_RUN}":
            return _response(self._historical_required_run())
        if suffix == "actions/workflows/source-ci.yml/runs":
            query = urllib.parse.parse_qs(parsed.query)
            head_sha = query.get("head_sha", [None])[0]
            candidates = {
                ACTIVATION: self.activation_run,
                CONTINUATION: self.continuation_run,
                REPAIR: self.repair_run,
                PUBLICATION: self.publication_run,
            }
            selected = candidates.get(head_sha)
            runs = [selected] if selected is not None else []
            return _response({"total_count": len(runs), "workflow_runs": runs})
        if suffix == "actions/workflows/source-published-release-recovery.yml/runs":
            return _response(
                {"total_count": 1, "workflow_runs": [self._recovery_run()]}
            )
        if suffix == f"actions/runs/{PREFLIGHT_RUN}/artifacts":
            return _response(
                {
                    "artifacts": [
                        {
                            "digest": ARTIFACT_DIGEST,
                            "expired": False,
                            "expires_at": PREFLIGHT_EXPIRES_AT,
                            "id": ARTIFACT_ID,
                            "name": ARTIFACT,
                            "size_in_bytes": 4096,
                            "workflow_run": {
                                "head_branch": "main",
                                "head_sha": SOURCE,
                                "id": PREFLIGHT_RUN,
                            },
                        }
                    ],
                    "total_count": 1,
                }
            )
        if suffix == f"actions/runs/{RECOVERY_RUN}/artifacts":
            return _response(
                {
                    "artifacts": [
                        {
                            "digest": RECOVERY_ARTIFACT_DIGEST,
                            "expired": False,
                            "expires_at": PREFLIGHT_EXPIRES_AT,
                            "id": RECOVERY_ARTIFACT_ID,
                            "name": RECOVERY_ARTIFACT,
                            "size_in_bytes": 8192,
                            "workflow_run": {
                                "head_branch": "main",
                            "head_sha": REPAIR,
                                "id": RECOVERY_RUN,
                            },
                        }
                    ],
                    "total_count": 1,
                }
            )
        if suffix == f"actions/runs/{FAILED_RECOVERY_RUN}/artifacts":
            return _response(
                {
                    "artifacts": [
                        {
                            "digest": FAILED_RECOVERY_ARTIFACT_DIGEST,
                            "expired": False,
                            "expires_at": PREFLIGHT_EXPIRES_AT,
                            "id": FAILED_RECOVERY_ARTIFACT_ID,
                            "name": FAILED_RECOVERY_ARTIFACT,
                            "size_in_bytes": 595,
                            "workflow_run": {
                                "head_branch": "main",
                                "head_sha": ACTIVATION,
                                "id": FAILED_RECOVERY_RUN,
                            },
                        }
                    ],
                    "total_count": 1,
                }
            )
        if suffix == f"actions/runs/{FAILED_PUBLICATION_RUN}/artifacts":
            return _response(
                {
                    "artifacts": [
                        {
                            "digest": FAILED_PUBLICATION_ARTIFACT_DIGEST,
                            "expired": False,
                            "expires_at": PREFLIGHT_EXPIRES_AT,
                            "id": FAILED_PUBLICATION_ARTIFACT_ID,
                            "name": FAILED_PUBLICATION_ARTIFACT,
                            "size_in_bytes": 8192,
                            "workflow_run": {
                                "head_branch": "main",
                                "head_sha": CONTINUATION,
                                "id": FAILED_PUBLICATION_RUN,
                            },
                        }
                    ],
                    "total_count": 1,
                }
            )
        if suffix == "branches/main":
            return _response(
                {
                    "name": "main",
                    "protected": True,
                    "protection": {
                        "required_status_checks": {
                            "checks": self.branch_checks,
                            "contexts": self.branch_contexts,
                            "enforcement_level": "everyone",
                        }
                    },
                }
            )
        if suffix == f"check-runs/{HISTORICAL_REQUIRED_CHECK}":
            return _response(self._required_check(historical=True))
        if suffix == f"check-runs/{RECOVERED_REQUIRED_CHECK}":
            return _response(self._required_check(historical=False))
        if suffix == f"commits/{RELEASE}/check-runs":
            query = urllib.parse.parse_qs(parsed.query)
            self.assertEqualQuery(
                query,
                {
                    "app_id": str(
                        approval.validation_recovery.REQUIRED_CHECK_APP_ID
                    ),
                    "check_name": approval.validation_recovery.REQUIRED_CHECK_CONTEXT,
                    "per_page": "100",
                },
            )
            filter_value = query.get("filter", [None])[0]
            historical = self._required_check(historical=True)
            recovered = self._required_check(historical=False)
            current_id = self.latest_required_check_id
            if current_id is None:
                current_id = (
                    RECOVERED_REQUIRED_CHECK
                    if self.required_check_published
                    else HISTORICAL_REQUIRED_CHECK
                )
            if filter_value == "latest":
                values = [historical]
                if current_id == RECOVERED_REQUIRED_CHECK:
                    values.insert(0, recovered)
                    values = [*self.post_publication_required_checks, *values]
                return _response(
                    {"check_runs": values, "total_count": len(values)}
                )
            if filter_value == "all":
                values = [historical]
                if self.required_check_published:
                    values.insert(0, recovered)
                    values = [*self.post_publication_required_checks, *values]
                return _response(
                    {"check_runs": values, "total_count": len(values)}
                )
            raise AssertionError(f"unexpected check filter: {filter_value}")
        if suffix == "check-runs" and method == "POST" and body is not None:
            if self.deny_check_publication:
                return sync.HttpResponse(
                    403, b'{"message":"private check publication body"}\n'
                )
            self.published_check_payload = json.loads(body)
            self.required_check_published = True
            return sync.HttpResponse(
                201,
                (
                    json.dumps(
                        self._required_check(historical=False),
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode(),
            )
        if suffix == f"issues/{PR_NUMBER}/comments":
            if method == "GET":
                if self.ready_mode:
                    return _response([])
                return _response(
                    [
                        {
                            "body": approval.marker(
                                approval.RECOVERY_READY_PREFIX,
                                self.recovery_payload,
                            ),
                            "created_at": "2026-09-09T12:02:00Z",
                            "id": 704,
                            "user": {"login": approval.BOT_LOGIN},
                        }
                    ]
                )
            if method == "POST" and body is not None:
                posted = json.loads(body)
                return sync.HttpResponse(
                    201,
                    (
                        json.dumps(
                            {
                                "body": posted["body"],
                                "id": 704,
                                "user": {"login": approval.BOT_LOGIN},
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode(),
                )
        raise AssertionError(f"unexpected request: {method} {url}")

    @staticmethod
    def assertEqualQuery(
        query: Mapping[str, list[str]], expected_without_filter: Mapping[str, str]
    ) -> None:
        for key, expected in expected_without_filter.items():
            if query.get(key) != [expected]:
                raise AssertionError(f"unexpected {key} query: {query}")


class UpdatedRecoveryTransport(RecoveryTransport):
    """GitHub fixture for an updated PR #52 head and immutable v0.6.2."""

    def __init__(
        self,
        *,
        ready: bool,
        current_payload: dict[str, Any] | None = None,
        historical_payload: dict[str, Any] | None = None,
        merge_payload: dict[str, Any] | None = None,
        include_merge_marker: bool = True,
    ) -> None:
        super().__init__(ready=ready)
        self.current_payload = current_payload
        self.historical_payload = historical_payload or _recovery_ready_payload()
        self.merge_payload = merge_payload or current_payload
        self.include_merge_marker = include_merge_marker
        self.current_sync_base = SYNC_REPAIR
        self.current_sync_head = SYNC_HEAD
        self.current_sync_tree = SYNC_HEAD_TREE
        self.current_sync_merge = SYNC_MERGE
        self.previous_sync_head = RELEASE
        self.previous_sync_tree = "e" * 40
        self.main_tip = self.current_sync_base if ready else self.current_sync_merge
        self.sync_ref = self.current_sync_head
        self.check_association_head = self.current_sync_head
        self.check_association_base = self.current_sync_base
        self.pr_mergeable = True
        self.pr_mergeable_state = "clean"
        self.native_run_conclusion = "success"
        self.native_check_conclusion = "success"
        self.current_native_run = SYNC_NATIVE_RUN
        self.current_native_run_number = 89
        self.current_native_check = SYNC_NATIVE_CHECK
        self.current_native_suite = SYNC_NATIVE_SUITE
        self.current_native_node = SYNC_NATIVE_NODE
        self.current_native_completed_at = SYNC_NATIVE_COMPLETED_AT
        self.native_check_head = self.current_sync_head
        self.native_check_tree = self.current_sync_tree
        self.sync_repair_run = {
            "conclusion": "success",
            "event": "push",
            "head_branch": "main",
            "head_repository": {"full_name": REPOSITORY},
            "head_sha": SYNC_REPAIR,
            "id": SYNC_REPAIR_RUN,
            "path": ".github/workflows/source-ci.yml@refs/heads/main",
            "repository": {"full_name": REPOSITORY},
            "run_attempt": 1,
            "run_number": 88,
            "status": "completed",
        }

    @staticmethod
    def _sync_repair_pr() -> dict[str, Any]:
        return {
            "base": {
                "ref": "main",
                "repo": {"full_name": REPOSITORY},
                "sha": PUBLICATION,
            },
            "draft": False,
            "head": {
                "ref": "codex/separate-sync-release-identity",
                "repo": {"full_name": REPOSITORY},
                "sha": SYNC_REPAIR_HEAD,
            },
            "html_url": f"https://github.com/{REPOSITORY}/pull/57",
            "merge_commit_sha": SYNC_REPAIR,
            "merged_at": "2026-09-12T11:00:00Z",
            "merged_by": {"login": OWNER},
            "number": 57,
            "state": "closed",
            "title": "Separate sync and immutable release identities",
            "user": {"login": OWNER},
        }

    def _sync_pr(self) -> dict[str, Any]:
        merged = not self.ready_mode
        return {
            "base": {
                "ref": "main",
                "repo": {"full_name": REPOSITORY},
                "sha": self.current_sync_base,
            },
            "draft": False,
            "head": {
                "ref": f"sync/platform-v{VERSION}",
                "repo": {"full_name": REPOSITORY},
                "sha": self.current_sync_head,
            },
            "html_url": f"https://github.com/{REPOSITORY}/pull/{PR_NUMBER}",
            "merge_commit_sha": self.current_sync_merge if merged else None,
            "mergeable": self.pr_mergeable,
            "mergeable_state": self.pr_mergeable_state,
            "merged": merged,
            "merged_at": "2026-09-12T12:20:00Z" if merged else None,
            "merged_by": {"login": OWNER} if merged else None,
            "number": PR_NUMBER,
            "state": "closed" if merged else "open",
            "title": f"Sync platform release v{VERSION}",
            "user": {"login": OWNER},
        }

    def _native_run(self) -> dict[str, Any]:
        return {
            "conclusion": self.native_run_conclusion,
            "event": "pull_request",
            "head_branch": f"sync/platform-v{VERSION}",
            "head_repository": {"full_name": REPOSITORY},
            "head_sha": self.current_sync_head,
            "id": self.current_native_run,
            "path": ".github/workflows/source-ci.yml@refs/pull/52/merge",
            "pull_requests": [{"number": PR_NUMBER}],
            "repository": {"full_name": REPOSITORY},
            "run_attempt": 1,
            "run_number": self.current_native_run_number,
            "status": "completed",
        }

    def _native_check(self) -> dict[str, Any]:
        return {
            "app": {
                "id": approval.validation_recovery.REQUIRED_CHECK_APP_ID,
                "slug": approval.validation_recovery.REQUIRED_CHECK_APP_SLUG,
            },
            "check_suite": {"id": self.current_native_suite},
            "completed_at": self.current_native_completed_at,
            "conclusion": self.native_check_conclusion,
            "details_url": (
                f"https://github.com/{REPOSITORY}/actions/runs/"
                f"{self.current_native_run}/job/{self.current_native_check}"
            ),
            "external_id": "updated-sync-native-required-job",
            "head_sha": self.native_check_head,
            "id": self.current_native_check,
            "name": approval.validation_recovery.REQUIRED_CHECK_CONTEXT,
            "node_id": self.current_native_node,
            "pull_requests": self._required_check_association(),
            "started_at": "2026-09-12T12:04:00Z",
            "status": "completed",
        }

    def request(self, method, url, headers, body, timeout):
        parsed = urllib.parse.urlsplit(url)
        suffix = parsed.path.removeprefix(f"/repos/{REPOSITORY}/")
        if parsed.path == "/graphql" and body is not None:
            request = json.loads(body)
            variables = request.get("variables", {})
            if variables.get("checkRunId") != self.current_native_node:
                return super().request(method, url, headers, body, timeout)
            self.calls.append((method, parsed.path))
            check = self._native_check()
            return _response(
                {
                    "data": {
                        "node": {
                            "__typename": "CheckRun",
                            "checkSuite": {
                                "commit": {"oid": self.native_check_head},
                                "databaseId": self.current_native_suite,
                            },
                            "conclusion": str(check["conclusion"]).upper(),
                            "databaseId": self.current_native_check,
                            "detailsUrl": check["details_url"],
                            "externalId": check["external_id"],
                            "isRequired": True,
                            "name": check["name"],
                            "repository": {"nameWithOwner": REPOSITORY},
                            "status": "COMPLETED",
                        },
                        "repository": {
                            "nameWithOwner": REPOSITORY,
                            "pullRequest": {
                                "headRefOid": self.current_sync_head,
                                "number": PR_NUMBER,
                                "state": "OPEN" if self.ready_mode else "MERGED",
                            },
                        },
                    }
                }
            )
        handled = True
        if suffix == f"pulls/{PR_NUMBER}" and method == "GET":
            response = _response(self._sync_pr())
        elif suffix == "pulls/57" and method == "GET":
            response = _response(self._sync_repair_pr())
        elif suffix == f"commits/{SYNC_REPAIR}":
            response = _response(
                {
                    "commit": {"tree": {"sha": SYNC_REPAIR_TREE}},
                    "parents": [
                        {"sha": PUBLICATION},
                        {"sha": SYNC_REPAIR_HEAD},
                    ],
                    "sha": SYNC_REPAIR,
                }
            )
        elif suffix == f"commits/{SYNC_REPAIR_HEAD}":
            response = _response(
                {
                    "commit": {"tree": {"sha": SYNC_REPAIR_TREE}},
                    "parents": [{"sha": PUBLICATION}],
                    "sha": SYNC_REPAIR_HEAD,
                }
            )
        elif suffix == f"commits/{SYNC_REPAIR}/pulls":
            association = self._sync_repair_pr()
            association.pop("merged_by")
            response = _response([association])
        elif (
            self.previous_sync_head != RELEASE
            and suffix == f"commits/{self.previous_sync_head}"
        ):
            response = _response(
                {
                    "commit": {"tree": {"sha": self.previous_sync_tree}},
                    "parents": [{"sha": RELEASE}, {"sha": SYNC_REPAIR}],
                    "sha": self.previous_sync_head,
                }
            )
        elif suffix == f"commits/{self.current_sync_head}":
            response = _response(
                {
                    "commit": {"tree": {"sha": self.native_check_tree}},
                    "parents": [
                        {"sha": self.previous_sync_head},
                        {"sha": self.current_sync_base},
                    ],
                    "sha": self.current_sync_head,
                }
            )
        elif suffix == f"commits/{self.current_sync_merge}":
            message = f"Sync platform release v{VERSION}"
            if self.include_merge_marker and self.merge_payload is not None:
                message += "\n\n" + approval.marker(
                    approval.CURRENT_RECOVERY_APPROVAL_PREFIX,
                    approval.approval_payload(self.merge_payload),
                )
            response = _response(
                {
                    "commit": {
                        "message": message,
                        "tree": {"sha": self.native_check_tree},
                    },
                    "parents": [
                        {"sha": self.current_sync_base},
                        {"sha": self.current_sync_head},
                    ],
                    "sha": self.current_sync_merge,
                }
            )
        elif suffix == f"git/ref/heads/sync/platform-v{VERSION}":
            response = _response(
                {
                    "ref": f"refs/heads/sync/platform-v{VERSION}",
                    "object": {"sha": self.sync_ref, "type": "commit"},
                }
            )
        elif suffix == "git/ref/heads/main":
            response = _response(
                {
                    "ref": "refs/heads/main",
                    "object": {"sha": self.main_tip, "type": "commit"},
                }
            )
        elif suffix == f"actions/runs/{SYNC_REPAIR_RUN}":
            response = _response(self.sync_repair_run)
        elif suffix == f"actions/runs/{self.current_native_run}":
            response = _response(self._native_run())
        elif suffix == f"actions/runs/{self.current_native_run}/jobs":
            check = self._native_check()
            response = _response(
                {
                    "jobs": [
                        {
                            "conclusion": self.native_check_conclusion,
                            "head_sha": self.current_sync_head,
                            "html_url": check["details_url"],
                            "id": self.current_native_check,
                            "name": approval.validation_recovery.REQUIRED_CHECK_CONTEXT,
                            "run_attempt": 1,
                            "run_id": self.current_native_run,
                            "status": "completed",
                            "workflow_name": "Validate PR",
                        }
                    ],
                    "total_count": 1,
                }
            )
        elif suffix == f"check-runs/{self.current_native_check}":
            response = _response(self._native_check())
        elif suffix == "actions/workflows/source-ci.yml/runs":
            query = urllib.parse.parse_qs(parsed.query)
            head_sha = query.get("head_sha", [None])[0]
            if head_sha == SYNC_REPAIR:
                runs = [self.sync_repair_run]
            elif head_sha == self.current_sync_head:
                runs = [self._native_run()]
            else:
                return super().request(method, url, headers, body, timeout)
            response = _response({"total_count": len(runs), "workflow_runs": runs})
        elif suffix == f"issues/{PR_NUMBER}/comments":
            comments = [
                {
                    "body": approval.marker(
                        approval.RECOVERY_READY_PREFIX, self.historical_payload
                    ),
                    "created_at": "2026-09-09T12:02:00Z",
                    "id": 704,
                    "user": {"login": approval.BOT_LOGIN},
                }
            ]
            if method == "GET":
                if self.current_payload is not None:
                    comments.append(
                        {
                            "body": approval.marker(
                                approval.CURRENT_RECOVERY_READY_PREFIX,
                                self.current_payload,
                            ),
                            "created_at": "2026-09-12T12:10:00Z",
                            "id": 705,
                            "user": {"login": approval.BOT_LOGIN},
                        }
                    )
                response = _response(comments)
            elif method == "POST" and body is not None:
                posted = json.loads(body)
                response = sync.HttpResponse(
                    201,
                    (
                        json.dumps(
                            {
                                "body": posted["body"],
                                "id": 705,
                                "user": {"login": approval.BOT_LOGIN},
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode(),
                )
            else:
                raise AssertionError(f"unexpected comments request: {method}")
        else:
            handled = False
            response = None
        if not handled:
            return super().request(method, url, headers, body, timeout)
        self.calls.append((method, parsed.path))
        return response


class FinalRecoveryTransport(UpdatedRecoveryTransport):
    """Fixture for the reviewed PR #59 merge and PR #52's second update."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.current_sync_base = ASSOCIATION_REPAIR
        self.current_sync_head = NEXT_SYNC_HEAD
        self.current_sync_tree = NEXT_SYNC_HEAD_TREE
        self.current_sync_merge = NEXT_SYNC_MERGE
        self.previous_sync_head = SYNC_HEAD
        self.previous_sync_tree = SYNC_HEAD_TREE
        self.main_tip = self.current_sync_base if self.ready_mode else self.current_sync_merge
        self.sync_ref = self.current_sync_head
        self.check_association_head = self.current_sync_head
        self.check_association_base = self.current_sync_base
        self.current_native_run = NEXT_SYNC_NATIVE_RUN
        self.current_native_run_number = 91
        self.current_native_check = NEXT_SYNC_NATIVE_CHECK
        self.current_native_suite = NEXT_SYNC_NATIVE_SUITE
        self.current_native_node = NEXT_SYNC_NATIVE_NODE
        self.current_native_completed_at = NEXT_SYNC_NATIVE_COMPLETED_AT
        self.native_check_head = self.current_sync_head
        self.native_check_tree = self.current_sync_tree
        self.association_repair_run = {
            "conclusion": "success",
            "event": "push",
            "head_branch": "main",
            "head_repository": {"full_name": REPOSITORY},
            "head_sha": ASSOCIATION_REPAIR,
            "id": ASSOCIATION_REPAIR_RUN,
            "path": ".github/workflows/source-ci.yml@refs/heads/main",
            "repository": {"full_name": REPOSITORY},
            "run_attempt": 1,
            "run_number": 90,
            "status": "completed",
        }

    @staticmethod
    def _association_repair_pr() -> dict[str, Any]:
        return {
            "base": {
                "ref": "main",
                "repo": {"full_name": REPOSITORY},
                "sha": SYNC_REPAIR,
            },
            "draft": False,
            "head": {
                "ref": "codex/fix-historical-check-pr-association",
                "repo": {"full_name": REPOSITORY},
                "sha": ASSOCIATION_REPAIR_HEAD,
            },
            "html_url": f"https://github.com/{REPOSITORY}/pull/59",
            "merge_commit_sha": ASSOCIATION_REPAIR,
            "merged_at": "2026-09-13T11:00:00Z",
            "merged_by": {"login": OWNER},
            "number": 59,
            "state": "closed",
            "title": "Fix historical check PR association",
            "user": {"login": OWNER},
        }

    def request(self, method, url, headers, body, timeout):
        parsed = urllib.parse.urlsplit(url)
        suffix = parsed.path.removeprefix(f"/repos/{REPOSITORY}/")
        if suffix == "pulls/59" and method == "GET":
            response = _response(self._association_repair_pr())
        elif suffix == f"commits/{ASSOCIATION_REPAIR}":
            response = _response(
                {
                    "commit": {"tree": {"sha": ASSOCIATION_REPAIR_TREE}},
                    "parents": [
                        {"sha": SYNC_REPAIR},
                        {"sha": ASSOCIATION_REPAIR_HEAD},
                    ],
                    "sha": ASSOCIATION_REPAIR,
                }
            )
        elif suffix == f"commits/{ASSOCIATION_REPAIR_HEAD}":
            response = _response(
                {
                    "commit": {"tree": {"sha": ASSOCIATION_REPAIR_TREE}},
                    "parents": [{"sha": SYNC_REPAIR}],
                    "sha": ASSOCIATION_REPAIR_HEAD,
                }
            )
        elif suffix == f"commits/{ASSOCIATION_REPAIR}/pulls":
            association = self._association_repair_pr()
            association.pop("merged_by")
            response = _response([association])
        elif suffix == f"actions/runs/{ASSOCIATION_REPAIR_RUN}":
            response = _response(self.association_repair_run)
        elif suffix == "actions/workflows/source-ci.yml/runs":
            query = urllib.parse.parse_qs(parsed.query)
            if query.get("head_sha", [None])[0] != ASSOCIATION_REPAIR:
                return super().request(method, url, headers, body, timeout)
            response = _response(
                {"total_count": 1, "workflow_runs": [self.association_repair_run]}
            )
        else:
            return super().request(method, url, headers, body, timeout)
        self.calls.append((method, parsed.path))
        return response


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

    def recover_ready_with(self, transport: RecoveryTransport):
        with tempfile.TemporaryDirectory() as temp:
            return approval.ready_recovery(
                _recovery_contract(),
                TOKEN,
                owner=OWNER,
                repository=REPOSITORY,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path(temp) / "recovery.json",
                feature_readiness=_published_readiness(),
                activation_readiness=_published_readiness(
                    pull_request=53,
                    feature_head=ACTIVATION_HEAD,
                    merge_source_commit=ACTIVATION,
                ),
                continuation_readiness=_published_readiness(
                    pull_request=54,
                    feature_head=CONTINUATION_HEAD,
                    merge_source_commit=CONTINUATION,
                ),
                repair_readiness=_published_readiness(
                    pull_request=55,
                    feature_head=REPAIR_HEAD,
                    merge_source_commit=REPAIR,
                ),
                publication_repair=_publication_repair_evidence(),
                publication_readiness=_published_readiness(
                    pull_request=56,
                    feature_head=PUBLICATION_HEAD,
                    merge_source_commit=PUBLICATION,
                ),
                validation_attestation=_recovery_attestation(),
                validation_artifact_id=RECOVERY_ARTIFACT_ID,
                validation_artifact_name=RECOVERY_ARTIFACT,
                validation_artifact_digest=RECOVERY_ARTIFACT_DIGEST,
                transport=transport,
                sleeper=lambda _: None,
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

    def test_readiness_never_offers_approval_without_deployment_evidence_runway(self):
        class NearExpiryTransport(ReadyTransport):
            def request(self, method, url, headers, body, timeout):
                response = super().request(method, url, headers, body, timeout)
                if urllib.parse.urlsplit(url).path.endswith(f"/actions/runs/{PREFLIGHT_RUN}/artifacts"):
                    value = json.loads(response.body)
                    value["artifacts"][0]["expires_at"] = (
                        approval.dt.datetime.now(approval.dt.timezone.utc)
                        + approval.dt.timedelta(minutes=60)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
                    return _response(value)
                return response

        transport = NearExpiryTransport()
        with tempfile.TemporaryDirectory() as temporary:
            config = approval._config(
                owner=OWNER, repository=REPOSITORY, version=VERSION,
                source_commit=SOURCE, release_commit=RELEASE,
                api_url="https://api.github.com", server_url="https://github.com",
                output=Path(temporary) / "ready.json",
            )
            with self.assertRaisesRegex(approval.ApprovalError, "90-minute"):
                approval.ready(
                    config, TOKEN, number=PR_NUMBER, feature_readiness=_published_readiness(),
                    main_validation_run_id=MAIN_RUN, main_validation_run_attempt=1,
                    manifest_sha256=MANIFEST, preflight_run_id=PREFLIGHT_RUN,
                    preflight_run_attempt=1, preflight_artifact=ARTIFACT,
                    transport=transport, sleeper=lambda _: None, polls=1,
                )
        self.assertFalse(any(method == "POST" for method, _path in transport.calls))

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

    def test_recovery_ready_binds_unchanged_release_harness_and_failed_run(self):
        transport = RecoveryTransport(ready=True)
        report = self.recover_ready_with(transport)
        ready = report["ready"]
        self.assertEqual(ready["schema_version"], approval.RECOVERY_SCHEMA_VERSION)
        self.assertEqual(ready["source_commit"], SOURCE)
        self.assertEqual(ready["release_commit"], RELEASE)
        self.assertEqual(ready["sync_validation_run_id"], RECOVERY_RUN)
        self.assertEqual(
            ready["recovery_validation"]["activation_readiness"]["feature_pr"],
            {"head_sha": ACTIVATION_HEAD, "number": 53},
        )
        self.assertEqual(
            ready["recovery_validation"]["continuation_readiness"]["feature_pr"],
            {"head_sha": CONTINUATION_HEAD, "number": 54},
        )
        self.assertEqual(
            ready["recovery_validation"]["repair_readiness"]["feature_pr"],
            {"head_sha": REPAIR_HEAD, "number": 55},
        )
        self.assertEqual(
            ready["recovery_validation"]["publication_repair_readiness"][
                "feature_pr"
            ],
            {"head_sha": PUBLICATION_HEAD, "number": 56},
        )
        self.assertEqual(
            ready["recovery_validation"]["attestation"]["harness"]["commit"],
            REPAIR,
        )
        self.assertEqual(
            ready["recovery_validation"]["failed_publication"]["run_id"],
            FAILED_PUBLICATION_RUN,
        )
        self.assertEqual(
            ready["recovery_validation"]["artifact_digest"],
            RECOVERY_ARTIFACT_DIGEST,
        )
        required_check = ready["recovery_validation"]["required_check"]
        self.assertEqual(required_check["head_sha"], RELEASE)
        self.assertEqual(
            required_check["context"],
            approval.validation_recovery.REQUIRED_CHECK_CONTEXT,
        )
        self.assertEqual(
            required_check["historical_check_run_id"],
            HISTORICAL_REQUIRED_CHECK,
        )
        self.assertEqual(
            required_check["check_run_node_id"], RECOVERED_REQUIRED_NODE
        )
        self.assertEqual(
            required_check["check_suite_id"], RECOVERED_REQUIRED_SUITE
        )
        self.assertEqual(
            required_check["completed_at"], RECOVERED_REQUIRED_COMPLETED_AT
        )
        self.assertEqual(required_check["required_pull_request"], PR_NUMBER)
        self.assertEqual(required_check["lanes"], _recovery_lane_records())
        self.assertIsNone(transport.published_check_payload)
        self.assertNotIn(
            ("POST", f"/repos/{REPOSITORY}/check-runs"),
            transport.calls,
        )
        comment_post = transport.calls.index(
            ("POST", f"/repos/{REPOSITORY}/issues/{PR_NUMBER}/comments")
        )
        check_read = transport.calls.index(
            ("GET", f"/repos/{REPOSITORY}/check-runs/{RECOVERED_REQUIRED_CHECK}")
        )
        self.assertLess(check_read, comment_post)
        self.assertEqual(ready["approval_nonce"], approval._nonce(ready))
        self.assertTrue(
            report["approval_marker"].startswith(
                approval.RECOVERY_APPROVAL_PREFIX
            )
        )
        self.assertGreaterEqual(transport.calls.count(("POST", "/graphql")), 2)

    def test_recovery_check_binding_names_the_digest_repair(self) -> None:
        binding, _ = approval._recovery_check_binding(
            _recovery_contract(),
            _recovery_attestation(),
            {
                "digest": RECOVERY_ARTIFACT_DIGEST,
                "id": RECOVERY_ARTIFACT_ID,
                "name": RECOVERY_ARTIFACT,
            },
            _recovery_lane_records(),
        )

        self.assertEqual(
            binding["schema_version"],
            approval.RECOVERY_CHECK_BINDING_SCHEMA_VERSION,
        )
        self.assertEqual(
            binding["repair"],
            _recovery_attestation()["repair"],
        )

    def test_recovery_lanes_accept_custom_run_title_but_reject_other_run(self):
        accepted = RecoveryTransport(ready=True)
        self.assertNotEqual(
            accepted.recovery_workflow_title, approval.RECOVERY_WORKFLOW_NAME
        )
        self.recover_ready_with(accepted)

        rejected = RecoveryTransport(ready=True)
        original_jobs = rejected._recovery_jobs

        def jobs_from_other_run():
            jobs = original_jobs()
            jobs[0]["run_id"] = RECOVERY_RUN + 1
            return jobs

        rejected._recovery_jobs = jobs_from_other_run  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            approval.ApprovalError, "job identity changed"
        ):
            self.recover_ready_with(rejected)

    def test_recovery_required_check_accepts_multiple_latest_suites(self):
        transport = RecoveryTransport(ready=True)
        report = self.recover_ready_with(transport)
        required_check = report["ready"]["recovery_validation"]["required_check"]
        self.assertEqual(required_check["check_run_id"], RECOVERED_REQUIRED_CHECK)
        self.assertEqual(required_check["check_suite_id"], RECOVERED_REQUIRED_SUITE)
        self.assertNotEqual(RECOVERED_REQUIRED_SUITE, HISTORICAL_REQUIRED_SUITE)
        self.assertIn(
            ("GET", f"/repos/{REPOSITORY}/check-runs/{HISTORICAL_REQUIRED_CHECK}"),
            transport.calls,
        )

    def test_recovery_check_creation_records_canonical_github_url(self):
        transport = RecoveryTransport(ready=True)
        transport.check_association_base = REPAIR
        transport.required_check_published = False
        transport.graphql_missing_reads = 1
        config = approval._config(
            owner=OWNER,
            repository=REPOSITORY,
            version=VERSION,
            source_commit=SOURCE,
            release_commit=RELEASE,
            api_url="https://api.github.com",
            server_url="https://github.com",
            output=Path("unused-recovery-output.json"),
        )
        client = approval.GitHubClient(
            config.api_url,
            TOKEN,
            transport=transport,
            sleeper=lambda _: None,
        )
        recorded = approval._publish_recovery_check(
            client,
            config,
            _recovery_contract(),
            _recovery_attestation(),
            {
                "digest": RECOVERY_ARTIFACT_DIGEST,
                "id": RECOVERY_ARTIFACT_ID,
                "name": RECOVERY_ARTIFACT,
            },
            _recovery_lane_records(),
            work_actor="",
        )
        self.assertEqual(
            transport.published_check_payload["details_url"],
            f"https://github.com/{REPOSITORY}/actions/runs/{RECOVERY_RUN}",
        )
        self.assertEqual(
            recorded["details_url"],
            f"https://github.com/{REPOSITORY}/runs/{RECOVERED_REQUIRED_CHECK}",
        )
        self.assertIn(
            ("POST", f"/repos/{REPOSITORY}/check-runs"), transport.calls
        )
        self.assertGreaterEqual(transport.calls.count(("POST", "/graphql")), 3)

    def test_sanitized_live_check_fixtures_satisfy_exact_contract(self):
        fixture_root = PROJECT_ROOT / "tests/release/fixtures"
        rest = json.loads(
            (fixture_root / "github-check-run-103395196156.json").read_text(
                encoding="utf-8"
            )
        )
        historical = json.loads(
            (fixture_root / "github-check-run-102298823162.json").read_text(
                encoding="utf-8"
            )
        )
        history = json.loads(
            (
                fixture_root
                / "github-required-check-history-6074b965.json"
            ).read_text(encoding="utf-8")
        )
        graphql = json.loads(
            (
                fixture_root
                / "github-check-run-103395196156-graphql.json"
            ).read_text(encoding="utf-8")
        )
        branch = json.loads(
            (fixture_root / "github-main-required-checks.json").read_text(
                encoding="utf-8"
            )
        )
        contract = approval.validation_recovery.load_contract()
        release = contract["release"]
        config = approval._config(
            owner=OWNER,
            repository=REPOSITORY,
            version="0.6.2",
            source_commit=release["source_commit"],
            release_commit=release["commit"],
            api_url="https://api.github.com",
            server_url="https://github.com",
            output=Path("unused-live-fixture-output.json"),
        )

        class FixtureTransport:
            def request(self, method, url, headers, body, timeout):
                del headers, body, timeout
                path = urllib.parse.urlsplit(url).path
                if method == "GET" and path.endswith("/branches/main"):
                    return _response(branch)
                if method == "POST" and path == "/graphql":
                    return _response(graphql)
                raise AssertionError(f"unexpected fixture request: {method} {path}")

        client = approval.GitHubClient(
            config.api_url,
            TOKEN,
            transport=FixtureTransport(),
            sleeper=lambda _: None,
        )
        required = contract["partial_publication"]["required_check"]
        checked = approval._validate_required_check_run(
            rest,
            config,
            contract,
            check_run_id=required["check_run_id"],
            conclusion="success",
            details_url=required["details_url"],
            association_head=contract["sync"]["prior_head_commit"],
            association_base=contract["check_association_repair"]["base_commit"],
            external_id=required["external_id"],
        )
        failed = approval._validate_required_check_run(
            historical,
            config,
            contract,
            check_run_id=contract["required_check"]["historical_failure"][
                "check_run_id"
            ],
            conclusion="failure",
            details_url=contract["required_check"]["historical_failure"][
                "details_url"
            ],
            association_head=contract["sync"]["prior_head_commit"],
            association_base=contract["check_association_repair"]["base_commit"],
        )
        approval._verify_historical_check_is_current(
            [historical],
            config,
            contract,
            failed,
            association_head=contract["sync"]["prior_head_commit"],
            association_base=contract["check_association_repair"]["base_commit"],
        )
        self.assertTrue(
            approval._published_check_is_current(
                history["check_runs"],
                config,
                contract,
                checked,
                association_head=contract["sync"]["prior_head_commit"],
                association_base=contract["check_association_repair"][
                    "base_commit"
                ],
            )
        )
        self.assertTrue(
            approval._published_check_history_is_present(
                history["check_runs"],
                config,
                contract,
                checked,
                failed,
                association_head=contract["sync"]["prior_head_commit"],
                association_base=contract["check_association_repair"][
                    "base_commit"
                ],
            )
        )
        approval._verify_required_check_protection(client, config, contract)
        approval._verify_exact_required_check_graphql(
            client,
            config,
            contract,
            checked,
            pull_request_state="OPEN",
            pull_request_head=contract["sync"]["prior_head_commit"],
        )

        for section, field, wrong in (
            ("head", "sha", "f" * 40),
            ("head", "ref", "another-branch"),
            ("base", "sha", contract["sync_repair"]["base_commit"]),
            ("base", "ref", "another-base"),
        ):
            with self.subTest(section=section, field=field):
                conflicting = copy.deepcopy(rest)
                conflicting["pull_requests"][0][section][field] = wrong
                with self.assertRaisesRegex(
                    approval.ApprovalError, "check association"
                ):
                    approval._validate_required_check_run(
                        conflicting,
                        config,
                        contract,
                        check_run_id=required["check_run_id"],
                        conclusion="success",
                        details_url=required["details_url"],
                        association_head=contract["sync"]["prior_head_commit"],
                        association_base=contract["check_association_repair"][
                            "base_commit"
                        ],
                        external_id=required["external_id"],
                    )

        for label, conflicting in (
            (
                "pull request",
                {
                    **copy.deepcopy(rest),
                    "pull_requests": [
                        {
                            **copy.deepcopy(rest["pull_requests"][0]),
                            "number": 53,
                        }
                    ],
                },
            ),
            ("head repository", copy.deepcopy(rest)),
            ("base repository", copy.deepcopy(rest)),
        ):
            if label == "head repository":
                conflicting["pull_requests"][0]["head"]["repo"]["url"] = (
                    "https://api.github.com/repos/Fifty5D/another-repository"
                )
            elif label == "base repository":
                conflicting["pull_requests"][0]["base"]["repo"]["id"] = 0
            with self.subTest(label=label), self.assertRaisesRegex(
                approval.ApprovalError, "check association"
            ):
                approval._validate_required_check_run(
                    conflicting,
                    config,
                    contract,
                    check_run_id=required["check_run_id"],
                    conclusion="success",
                    details_url=required["details_url"],
                    association_head=contract["sync"]["prior_head_commit"],
                    association_base=contract["check_association_repair"][
                        "base_commit"
                    ],
                    external_id=required["external_id"],
                )

    def test_recovery_check_rejects_foreign_or_wrong_id_url(self):
        for details_url in (
            f"https://example.invalid/{REPOSITORY}/runs/{RECOVERED_REQUIRED_CHECK}",
            f"https://github.com/{REPOSITORY}/runs/{RECOVERED_REQUIRED_CHECK + 1}",
            f"https://github.com/{REPOSITORY}/actions/runs/{RECOVERY_RUN}",
        ):
            with self.subTest(details_url=details_url):
                transport = RecoveryTransport(ready=True)
                transport.recovered_check_overrides["details_url"] = details_url
                with self.assertRaisesRegex(
                    approval.ApprovalError, "validation check identity"
                ):
                    self.recover_ready_with(transport)

    def test_recovery_required_check_accepts_stronger_branch_protection(self):
        transport = RecoveryTransport(ready=True)
        transport.branch_checks.append(
            {"app_id": 4242, "context": "Additional policy check"}
        )
        transport.branch_contexts.append("Additional policy check")
        report = self.recover_ready_with(transport)
        self.assertEqual(
            report["ready"]["recovery_validation"]["required_check"][
                "check_run_id"
            ],
            RECOVERED_REQUIRED_CHECK,
        )
        self.assertFalse(
            any(method in {"PATCH", "PUT", "DELETE"} for method, _ in transport.calls)
        )

    def test_recovery_required_check_rejects_missing_context_or_app(self):
        cases = (
            ([{"app_id": 1, "context": "Another check"}], [
                approval.validation_recovery.REQUIRED_CHECK_CONTEXT
            ]),
            ([{
                "app_id": approval.validation_recovery.REQUIRED_CHECK_APP_ID,
                "context": approval.validation_recovery.REQUIRED_CHECK_CONTEXT,
            }], ["Another check"]),
        )
        for checks, contexts in cases:
            with self.subTest(checks=checks, contexts=contexts):
                transport = RecoveryTransport(ready=True)
                transport.branch_checks = checks
                transport.branch_contexts = contexts
                with self.assertRaisesRegex(
                    approval.ApprovalError, "branch protection changed"
                ):
                    self.recover_ready_with(transport)

    def test_recovery_check_graphql_visibility_poll_is_bounded(self):
        delayed = RecoveryTransport(ready=True)
        delayed.graphql_missing_reads = 1
        self.recover_ready_with(delayed)

        absent = RecoveryTransport(ready=True)
        absent.graphql_missing_reads = approval.RECOVERY_CHECK_POLLS
        with self.assertRaisesRegex(
            approval.ApprovalError, "bounded poll"
        ):
            self.recover_ready_with(absent)

    def test_recovery_check_graphql_conflicts_fail_immediately(self):
        cases = (
            {"databaseId": RECOVERED_REQUIRED_CHECK + 1},
            {
                "checkSuite": {
                    "commit": {"oid": RELEASE},
                    "databaseId": RECOVERED_REQUIRED_SUITE + 1,
                }
            },
            {
                "checkSuite": {
                    "commit": {"oid": SOURCE},
                    "databaseId": RECOVERED_REQUIRED_SUITE,
                }
            },
            {"isRequired": False},
        )
        for override in cases:
            with self.subTest(override=override):
                transport = RecoveryTransport(ready=True)
                transport.graphql_node_overrides = override
                with self.assertRaisesRegex(
                    approval.ApprovalError,
                    "protected pull-request requirement",
                ):
                    self.recover_ready_with(transport)
                self.assertEqual(
                    transport.calls.count(("POST", "/graphql")), 1
                )

        errored = RecoveryTransport(ready=True)
        errored.graphql_error_payload = {
            "data": None,
            "errors": [{"message": "sanitized fixture error"}],
        }
        with self.assertRaisesRegex(
            approval.ApprovalError, "invalid required-check GraphQL data"
        ):
            self.recover_ready_with(errored)

    def test_recovery_required_check_rejects_newer_conflicting_suite(self):
        transport = RecoveryTransport(ready=True)
        conflict = transport._required_check(historical=False)
        conflict.update(
            {
                "check_suite": {"id": RECOVERED_REQUIRED_SUITE + 1},
                "completed_at": "2026-09-09T12:06:00Z",
                "conclusion": "failure",
                "details_url": (
                    f"https://github.com/{REPOSITORY}/actions/runs/669/job/994"
                ),
                "external_id": "conflicting-required-check",
                "id": RECOVERED_REQUIRED_CHECK + 1,
                "node_id": "CR_conflicting_required_check",
                "started_at": "2026-09-09T12:05:30Z",
            }
        )
        transport.post_publication_required_checks.append(conflict)
        with self.assertRaisesRegex(
            approval.ApprovalError, "superseding or conflicting evidence"
        ):
            self.recover_ready_with(transport)
        self.assertNotIn(
            ("POST", f"/repos/{REPOSITORY}/issues/{PR_NUMBER}/comments"),
            transport.calls,
        )

    def test_recovery_required_check_must_be_required_for_exact_pr(self):
        transport = RecoveryTransport(ready=True)
        transport.non_required_check_nodes.add(RECOVERED_REQUIRED_NODE)
        with self.assertRaisesRegex(
            approval.ApprovalError, "protected pull-request requirement"
        ):
            self.recover_ready_with(transport)
        self.assertNotIn(
            ("POST", f"/repos/{REPOSITORY}/issues/{PR_NUMBER}/comments"),
            transport.calls,
        )

    def test_recovery_required_check_rejects_wrong_app_failure_and_stale(self):
        cases = (
            (
                "wrong-app",
                {"app": {"id": 1, "slug": "other-app"}},
                "validation check identity",
            ),
            ("wrong-context", {"name": "Another required check"}, "validation check identity"),
            ("wrong-sha", {"head_sha": SOURCE}, "validation check identity"),
            ("failure", {"conclusion": "failure"}, "validation check identity"),
            ("stale", {"conclusion": "stale"}, "validation check identity"),
            ("wrong-binding", {"external_id": "changed-binding"}, "binding is invalid"),
        )
        for name, override, message in cases:
            with self.subTest(name=name):
                transport = RecoveryTransport(ready=True)
                transport.recovered_check_overrides.update(override)
                with self.assertRaisesRegex(
                    approval.ApprovalError,
                    message,
                ):
                    self.recover_ready_with(transport)
                self.assertNotIn(
                    ("POST", f"/repos/{REPOSITORY}/issues/{PR_NUMBER}/comments"),
                    transport.calls,
                )

    def test_recovery_ready_rejects_altered_attestation_before_comment(self):
        attestation = _recovery_attestation()
        attestation["harness"]["files"][0]["sha256"] = "f" * 64
        transport = RecoveryTransport(ready=True)
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(
            approval.validation_recovery.ValidationRecoveryError
        ):
            approval.ready_recovery(
                _recovery_contract(),
                TOKEN,
                owner=OWNER,
                repository=REPOSITORY,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path(temp) / "recovery.json",
                feature_readiness=_published_readiness(),
                activation_readiness=_published_readiness(
                    pull_request=53,
                    feature_head=ACTIVATION_HEAD,
                    merge_source_commit=ACTIVATION,
                ),
                continuation_readiness=_published_readiness(
                    pull_request=54,
                    feature_head=CONTINUATION_HEAD,
                    merge_source_commit=CONTINUATION,
                ),
                repair_readiness=_published_readiness(
                    pull_request=55,
                    feature_head=REPAIR_HEAD,
                    merge_source_commit=REPAIR,
                ),
                publication_repair=_publication_repair_evidence(),
                publication_readiness=_published_readiness(
                    pull_request=56,
                    feature_head=PUBLICATION_HEAD,
                    merge_source_commit=PUBLICATION,
                ),
                validation_attestation=attestation,
                validation_artifact_id=RECOVERY_ARTIFACT_ID,
                validation_artifact_name=RECOVERY_ARTIFACT,
                validation_artifact_digest=RECOVERY_ARTIFACT_DIGEST,
                transport=transport,
                sleeper=lambda _: None,
            )
        self.assertNotIn(
            ("POST", f"/repos/{REPOSITORY}/issues/{PR_NUMBER}/comments"),
            transport.calls,
        )

    def test_recovery_activation_uses_association_then_full_pr_detail(self):
        transport = RecoveryTransport(ready=True)
        self.assertNotIn("merged_by", transport._activation_association())
        self.recover_ready_with(transport)
        association = transport.calls.index(
            ("GET", f"/repos/{REPOSITORY}/commits/{ACTIVATION}/pulls")
        )
        detail = transport.calls.index(
            ("GET", f"/repos/{REPOSITORY}/pulls/53")
        )
        self.assertLess(association, detail)
        continuation_association = transport.calls.index(
            ("GET", f"/repos/{REPOSITORY}/commits/{CONTINUATION}/pulls")
        )
        continuation_detail = transport.calls.index(
            ("GET", f"/repos/{REPOSITORY}/pulls/54")
        )
        self.assertLess(continuation_association, continuation_detail)
        repair_association = transport.calls.index(
            ("GET", f"/repos/{REPOSITORY}/commits/{REPAIR}/pulls")
        )
        repair_detail = transport.calls.index(
            ("GET", f"/repos/{REPOSITORY}/pulls/55")
        )
        self.assertLess(repair_association, repair_detail)

    def test_recovery_activation_denied_or_mismatched_detail_fails_closed(self):
        denied = RecoveryTransport(ready=True)
        denied.activation_detail_status = 403
        with self.assertRaises(sync.SyncPrError) as raised:
            self.recover_ready_with(denied)
        self.assertEqual(str(raised.exception), "GitHub GET failed with HTTP 403")
        self.assertNotIn("private activation detail", str(raised.exception))
        self.assertNotIn(
            ("POST", f"/repos/{REPOSITORY}/check-runs"), denied.calls
        )

        mismatches = {
            "merged_by": {"login": "untrusted-actor"},
            "merge_commit_sha": "f" * 40,
            "user": {"login": "untrusted-actor"},
        }
        for field, value in mismatches.items():
            with self.subTest(field=field):
                transport = RecoveryTransport(ready=True)
                transport.activation_detail[field] = value
                with self.assertRaisesRegex(
                    approval.ApprovalError,
                    "activation pull-request identity",
                ):
                    self.recover_ready_with(transport)
                self.assertNotIn(
                    ("POST", f"/repos/{REPOSITORY}/check-runs"),
                    transport.calls,
                )

        denied_continuation = RecoveryTransport(ready=True)
        denied_continuation.continuation_detail_status = 403
        with self.assertRaises(sync.SyncPrError) as raised:
            self.recover_ready_with(denied_continuation)
        self.assertEqual(str(raised.exception), "GitHub GET failed with HTTP 403")
        self.assertNotIn("private continuation detail", str(raised.exception))

    def test_recovery_required_check_denial_is_safe_and_blocks_readiness(self):
        transport = RecoveryTransport(ready=True)
        transport.check_association_base = REPAIR
        transport.required_check_published = False
        transport.deny_check_publication = True
        config = approval._config(
            owner=OWNER,
            repository=REPOSITORY,
            version=VERSION,
            source_commit=SOURCE,
            release_commit=RELEASE,
            api_url="https://api.github.com",
            server_url="https://github.com",
            output=Path("unused-recovery-output.json"),
        )
        client = approval.GitHubClient(
            config.api_url,
            TOKEN,
            transport=transport,
            sleeper=lambda _: None,
        )
        with self.assertRaises(sync.SyncPrError) as raised:
            approval._publish_recovery_check(
                client,
                config,
                _recovery_contract(),
                _recovery_attestation(),
                {
                    "digest": RECOVERY_ARTIFACT_DIGEST,
                    "id": RECOVERY_ARTIFACT_ID,
                    "name": RECOVERY_ARTIFACT,
                },
                _recovery_lane_records(),
                work_actor="",
            )
        message = str(raised.exception)
        self.assertEqual(
            message,
            "GitHub denied required release validation check publication with HTTP 403",
        )
        self.assertNotIn(TOKEN, message)
        self.assertNotIn("private check publication body", message)
        self.assertNotIn(
            ("POST", f"/repos/{REPOSITORY}/issues/{PR_NUMBER}/comments"),
            transport.calls,
        )

    def test_recovery_required_check_rejects_changed_protection_and_lane(self):
        changed_protection = RecoveryTransport(ready=True)
        changed_protection.branch_checks[0]["app_id"] = 1
        with self.assertRaisesRegex(
            approval.ApprovalError, "branch protection changed"
        ):
            self.recover_ready_with(changed_protection)

        failed_lane = RecoveryTransport(ready=True)
        original_jobs = failed_lane._recovery_jobs

        def jobs_with_failure():
            jobs = original_jobs()
            jobs[0]["conclusion"] = "failure"
            return jobs

        failed_lane._recovery_jobs = jobs_with_failure  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            approval.ApprovalError, "job evidence changed"
        ):
            self.recover_ready_with(failed_lane)

    def test_recovery_ready_rejects_changed_workflow_and_artifact(self):
        contract = _recovery_contract()
        for field, value, message in (
            ("path", ".github/workflows/other.yml", "workflow identity"),
            ("status", "in_progress", "did not complete successfully"),
        ):
            with self.subTest(field=field):
                transport = RecoveryTransport(ready=True)
                if field == "path":
                    transport.recovery_run_path = value
                else:
                    transport.recovery_run_status = value
                with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(
                    approval.ApprovalError, message
                ):
                    approval.ready_recovery(
                        contract,
                        TOKEN,
                        owner=OWNER,
                        repository=REPOSITORY,
                        api_url="https://api.github.com",
                        server_url="https://github.com",
                        output=Path(temp) / "recovery.json",
                        feature_readiness=_published_readiness(),
                        activation_readiness=_published_readiness(
                            pull_request=53,
                            feature_head=ACTIVATION_HEAD,
                            merge_source_commit=ACTIVATION,
                        ),
                        continuation_readiness=_published_readiness(
                            pull_request=54,
                            feature_head=CONTINUATION_HEAD,
                            merge_source_commit=CONTINUATION,
                        ),
                        repair_readiness=_published_readiness(
                            pull_request=55,
                            feature_head=REPAIR_HEAD,
                            merge_source_commit=REPAIR,
                        ),
                        publication_repair=_publication_repair_evidence(),
                        publication_readiness=_published_readiness(
                            pull_request=56,
                            feature_head=PUBLICATION_HEAD,
                            merge_source_commit=PUBLICATION,
                        ),
                        validation_attestation=_recovery_attestation(),
                        validation_artifact_id=RECOVERY_ARTIFACT_ID,
                        validation_artifact_name=RECOVERY_ARTIFACT,
                        validation_artifact_digest=RECOVERY_ARTIFACT_DIGEST,
                        transport=transport,
                        sleeper=lambda _: None,
                    )

        with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(
            approval.ApprovalError, "artifact digest"
        ):
            approval.ready_recovery(
                contract,
                TOKEN,
                owner=OWNER,
                repository=REPOSITORY,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=Path(temp) / "recovery.json",
                feature_readiness=_published_readiness(),
                activation_readiness=_published_readiness(
                    pull_request=53,
                    feature_head=ACTIVATION_HEAD,
                    merge_source_commit=ACTIVATION,
                ),
                continuation_readiness=_published_readiness(
                    pull_request=54,
                    feature_head=CONTINUATION_HEAD,
                    merge_source_commit=CONTINUATION,
                ),
                repair_readiness=_published_readiness(
                    pull_request=55,
                    feature_head=REPAIR_HEAD,
                    merge_source_commit=REPAIR,
                ),
                publication_repair=_publication_repair_evidence(),
                publication_readiness=_published_readiness(
                    pull_request=56,
                    feature_head=PUBLICATION_HEAD,
                    merge_source_commit=PUBLICATION,
                ),
                validation_attestation=_recovery_attestation(),
                validation_artifact_id=RECOVERY_ARTIFACT_ID,
                validation_artifact_name=RECOVERY_ARTIFACT,
                validation_artifact_digest="changed",
                transport=RecoveryTransport(ready=True),
                sleeper=lambda _: None,
            )

    def test_authorize_accepts_recovered_validation_after_activation_merge(self):
        payload = _recovery_ready_payload()
        transport = RecoveryTransport(ready=False, payload=payload)
        with tempfile.TemporaryDirectory() as temp, patch.object(
            approval.validation_recovery,
            "load_contract",
            return_value=_recovery_contract(),
        ):
            report = approval.authorize(
                _recovery_event(),
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
        self.assertEqual(report["validation_mode"], "published-release-recovery")
        self.assertEqual(report["release_commit"], RELEASE)
        self.assertEqual(report["source_commit"], SOURCE)
        self.assertEqual(report["sync_validation_run_id"], RECOVERY_RUN)
        self.assertEqual(
            report["recovery_validation"]["attestation"]["activation"][
                "activation_commit"
            ],
            ACTIVATION,
        )
        self.assertEqual(
            report["recovery_validation"]["required_check"]["check_run_id"],
            RECOVERED_REQUIRED_CHECK,
        )
        self.assertIn(
            ("GET", f"/repos/{REPOSITORY}/check-runs/{RECOVERED_REQUIRED_CHECK}"),
            transport.calls,
        )

    def test_authorize_recovery_rejects_superseded_required_check(self):
        payload = _recovery_ready_payload()
        transport = RecoveryTransport(ready=False, payload=payload)
        transport.latest_required_check_id = HISTORICAL_REQUIRED_CHECK
        with tempfile.TemporaryDirectory() as temp, patch.object(
            approval.validation_recovery,
            "load_contract",
            return_value=_recovery_contract(),
        ), self.assertRaisesRegex(
            approval.ApprovalError, "not a current suite result"
        ):
            approval.authorize(
                _recovery_event(),
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

    def test_authorize_recovery_rejects_wrong_merge_and_failed_validation(self):
        payload = _recovery_ready_payload()
        contract = _recovery_contract()
        transport = RecoveryTransport(ready=False, payload=payload)
        transport.main_tip = MERGE
        original_request = transport.request

        def wrong_merge(method, url, headers, body, timeout):
            if urllib.parse.urlsplit(url).path.endswith(f"/commits/{MERGE}"):
                response = json.loads(
                    original_request(method, url, headers, body, timeout).body
                )
                response["parents"][0]["sha"] = SOURCE
                return _response(response)
            return original_request(method, url, headers, body, timeout)

        transport.request = wrong_merge  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temp, patch.object(
            approval.validation_recovery, "load_contract", return_value=contract
        ), self.assertRaisesRegex(approval.ApprovalError, "tested source"):
            approval.authorize(
                _recovery_event(),
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

        transport = RecoveryTransport(ready=False, payload=payload)
        transport.recovery_run_conclusion = "success"
        with tempfile.TemporaryDirectory() as temp, patch.object(
            approval.validation_recovery, "load_contract", return_value=contract
        ), self.assertRaisesRegex(
            approval.ApprovalError, "did not complete successfully"
        ):
            approval.authorize(
                _recovery_event(),
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

    def test_retained_same_context_checks_do_not_qualify_a_behind_head(self):
        fixtures = PROJECT_ROOT / "tests/release/fixtures"
        behind = json.loads(
            (fixtures / "github-pr-52-behind.json").read_text(encoding="utf-8")
        )
        history = json.loads(
            (
                fixtures / "github-required-check-history-6074b965.json"
            ).read_text(encoding="utf-8")
        )
        checks = history["check_runs"]
        self.assertEqual(history["total_count"], 2)
        self.assertEqual(
            {item["conclusion"] for item in checks}, {"failure", "success"}
        )
        self.assertEqual(
            {item["check_suite"]["id"] for item in checks},
            {92911719432, 92911719969},
        )
        self.assertEqual(
            {item["name"] for item in checks},
            {approval.validation_recovery.REQUIRED_CHECK_CONTEXT},
        )
        config = approval._config(
            owner=OWNER,
            repository=REPOSITORY,
            version="0.6.2",
            source_commit="4f98e7cb559ee1a9b269ea1f94938678d2dac4df",
            release_commit=(
                "6074b965cbd2e6ab2630cd539ee455b8d419aef6"
            ),
            api_url="https://api.github.com",
            server_url="https://github.com",
            output=Path("unused-behind-report.json"),
        )
        with self.assertRaisesRegex(
            approval.ApprovalError,
            "Synchronization PR identity does not match the release",
        ):
            approval._validate_pr(
                behind,
                config,
                52,
                state="open",
                expected_head=config.release_commit,
                expected_base=(
                    "9f50567ae85a0bc9219c2c4f03e55dbb431e4cfb"
                ),
                require_mergeable=True,
            )

    def test_second_updated_sync_readiness_and_authorization_keep_v062_immutable(self):
        with patch(f"{__name__}.VERSION", "0.6.2"):
            contract = _current_recovery_contract()
            ready_transport = FinalRecoveryTransport(ready=True)
            with tempfile.TemporaryDirectory() as temp, patch.object(
                approval.validation_recovery,
                "load_contract",
                return_value=contract,
            ):
                temporary = Path(temp)
                sync_update_path = temporary / "sync-update.json"
                sync_repair_readiness_path = temporary / "sync-repair.json"
                association_readiness_path = temporary / "association-repair.json"
                for path, value in (
                    (
                        sync_update_path,
                        {
                            "recovery_id": contract["recovery_id"],
                            "schema_version": approval.validation_recovery.SCHEMA_VERSION,
                            "sync_update": _sync_update_report(),
                        },
                    ),
                    (
                        sync_repair_readiness_path,
                        _published_readiness(
                            pull_request=57,
                            feature_head=SYNC_REPAIR_HEAD,
                            merge_source_commit=SYNC_REPAIR,
                        ),
                    ),
                    (
                        association_readiness_path,
                        _published_readiness(
                            pull_request=59,
                            feature_head=ASSOCIATION_REPAIR_HEAD,
                            merge_source_commit=ASSOCIATION_REPAIR,
                        ),
                    ),
                ):
                    path.write_text(
                        approval._canonical(value) + "\n", encoding="ascii"
                    )
                output = temporary / "current-ready.json"
                stdout = io.StringIO()
                stderr = io.StringIO()
                client_class = approval.GitHubClient

                def ready_client(api_url, token, **kwargs):
                    del kwargs
                    return client_class(
                        api_url,
                        token,
                        transport=ready_transport,
                        sleeper=lambda _: None,
                    )

                with patch.object(approval, "GitHubClient", side_effect=ready_client):
                    result = approval.main(
                        [
                            "recover-ready-update",
                            "--owner",
                            OWNER,
                            "--repository",
                            REPOSITORY,
                            "--api-url",
                            "https://api.github.com",
                            "--server-url",
                            "https://github.com",
                            "--output",
                            str(output),
                            "--recovery-contract",
                            str(temporary / "contract.json"),
                            "--sync-update",
                            str(sync_update_path),
                            "--sync-repair-readiness",
                            str(sync_repair_readiness_path),
                            "--check-association-repair-readiness",
                            str(association_readiness_path),
                        ],
                        environment={"GITHUB_TOKEN": TOKEN},
                        stdout=stdout,
                        stderr=stderr,
                    )
                self.assertEqual(result, 0, msg=stderr.getvalue())
                ready_report = json.loads(output.read_text(encoding="ascii"))
                ready = ready_report["ready"]
                self.assertEqual(
                    ready_report["schema_version"],
                    approval.CURRENT_RECOVERY_SCHEMA_VERSION,
                )
                self.assertEqual(ready["release_commit"], RELEASE)
                self.assertEqual(ready["sync_base_commit"], ASSOCIATION_REPAIR)
                self.assertEqual(ready["sync_head_commit"], NEXT_SYNC_HEAD)
                self.assertNotEqual(
                    ready["release_commit"], ready["sync_head_commit"]
                )
                self.assertEqual(
                    ready["supersedes"], contract["historical_readiness"]
                )
                self.assertIn(
                    (
                        "POST",
                        f"/repos/{REPOSITORY}/issues/{PR_NUMBER}/comments",
                    ),
                    ready_transport.calls,
                )

                authorization_transport = FinalRecoveryTransport(
                    ready=False, current_payload=ready
                )
                event = {
                    "action": "closed",
                    "number": PR_NUMBER,
                    "pull_request": authorization_transport._sync_pr(),
                    "sender": {"login": OWNER},
                }
                event_path = temporary / "event.json"
                event_path.write_text(json.dumps(event), encoding="utf-8")
                authorization_output = temporary / "authorization.json"

                def authorization_client(api_url, token, **kwargs):
                    del kwargs
                    return client_class(
                        api_url,
                        token,
                        transport=authorization_transport,
                        sleeper=lambda _: None,
                    )

                stdout = io.StringIO()
                stderr = io.StringIO()
                with patch.object(
                    approval, "GitHubClient", side_effect=authorization_client
                ):
                    result = approval.main(
                        [
                            "authorize",
                            "--owner",
                            OWNER,
                            "--repository",
                            REPOSITORY,
                            "--api-url",
                            "https://api.github.com",
                            "--server-url",
                            "https://github.com",
                            "--output",
                            str(authorization_output),
                            "--actor",
                            OWNER,
                            "--triggering-actor",
                            OWNER,
                            "--run-attempt",
                            "1",
                            "--event",
                            str(event_path),
                        ],
                        environment={"GITHUB_TOKEN": TOKEN},
                        stdout=stdout,
                        stderr=stderr,
                    )
                self.assertEqual(result, 0, msg=stderr.getvalue())
                authorized = json.loads(
                    authorization_output.read_text(encoding="ascii")
                )
            self.assertEqual(
                authorized["validation_mode"],
                "published-release-sync-update",
            )
            self.assertEqual(authorized["release_commit"], RELEASE)
            self.assertEqual(authorized["sync_head_commit"], NEXT_SYNC_HEAD)
            self.assertEqual(authorized["merge_commit"], NEXT_SYNC_MERGE)
            self.assertEqual(
                authorized["manifest_sha256"], contract["release"]["manifest_sha256"]
            )

    def test_updated_sync_handoff_rejects_behind_stale_and_conflicting_state(self):
        with patch(f"{__name__}.VERSION", "0.6.2"):
            contract = _current_recovery_contract()
            repair_readiness = _published_readiness(
                pull_request=57,
                feature_head=SYNC_REPAIR_HEAD,
                merge_source_commit=SYNC_REPAIR,
            )
            association_readiness = _published_readiness(
                pull_request=59,
                feature_head=ASSOCIATION_REPAIR_HEAD,
                merge_source_commit=ASSOCIATION_REPAIR,
            )
            with patch.object(
                approval.validation_recovery,
                "load_contract",
                return_value=contract,
            ):
                behind = FinalRecoveryTransport(ready=True)
                behind.pr_mergeable_state = "behind"
                with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(
                    approval.ApprovalError, "Synchronization PR identity"
                ):
                    approval.ready_recovery_update(
                        contract,
                        TOKEN,
                        owner=OWNER,
                        repository=REPOSITORY,
                        api_url="https://api.github.com",
                        server_url="https://github.com",
                        output=Path(temp) / "behind.json",
                        sync_update=_sync_update_report(),
                        sync_repair_readiness=repair_readiness,
                        check_association_repair_readiness=association_readiness,
                        transport=behind,
                        sleeper=lambda _: None,
                        polls=1,
                    )
                self.assertNotIn(
                    (
                        "POST",
                        f"/repos/{REPOSITORY}/issues/{PR_NUMBER}/comments",
                    ),
                    behind.calls,
                )

                wrong_tree = copy.deepcopy(_sync_update_report())
                wrong_tree["sync_head_tree"] = "f" * 40
                with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(
                    approval.ApprovalError,
                    "Synchronization update commit identity is invalid",
                ):
                    approval.ready_recovery_update(
                        contract,
                        TOKEN,
                        owner=OWNER,
                        repository=REPOSITORY,
                        api_url="https://api.github.com",
                        server_url="https://github.com",
                        output=Path(temp) / "wrong-tree.json",
                        sync_update=wrong_tree,
                        sync_repair_readiness=repair_readiness,
                        check_association_repair_readiness=association_readiness,
                        transport=FinalRecoveryTransport(ready=True),
                        sleeper=lambda _: None,
                        polls=1,
                    )

                changed_prior = FinalRecoveryTransport(ready=True)
                original_request = changed_prior.request

                def altered_prior(method, url, headers, body, timeout):
                    response = original_request(method, url, headers, body, timeout)
                    if urllib.parse.urlsplit(url).path.endswith(
                        f"/commits/{SYNC_HEAD}"
                    ):
                        value = json.loads(response.body)
                        value["commit"]["tree"]["sha"] = "f" * 40
                        return _response(value)
                    return response

                changed_prior.request = altered_prior  # type: ignore[method-assign]
                with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(
                    approval.ApprovalError,
                    "Previously completed synchronization update changed",
                ):
                    approval.ready_recovery_update(
                        contract,
                        TOKEN,
                        owner=OWNER,
                        repository=REPOSITORY,
                        api_url="https://api.github.com",
                        server_url="https://github.com",
                        output=Path(temp) / "changed-prior.json",
                        sync_update=_sync_update_report(),
                        sync_repair_readiness=repair_readiness,
                        check_association_repair_readiness=association_readiness,
                        transport=changed_prior,
                        sleeper=lambda _: None,
                        polls=1,
                    )

                with tempfile.TemporaryDirectory() as temp:
                    valid_report = approval.ready_recovery_update(
                        contract,
                        TOKEN,
                        owner=OWNER,
                        repository=REPOSITORY,
                        api_url="https://api.github.com",
                        server_url="https://github.com",
                        output=Path(temp) / "valid.json",
                        sync_update=_sync_update_report(),
                        sync_repair_readiness=repair_readiness,
                        check_association_repair_readiness=association_readiness,
                        transport=FinalRecoveryTransport(ready=True),
                        sleeper=lambda _: None,
                        polls=1,
                    )
                    current = valid_report["ready"]
                    stale = copy.deepcopy(current)
                    stale["sync_base_commit"] = PUBLICATION
                    stale["approval_nonce"] = approval._nonce(stale)
                    rejected = FinalRecoveryTransport(
                        ready=False,
                        current_payload=current,
                        merge_payload=stale,
                    )
                    event = {
                        "action": "closed",
                        "number": PR_NUMBER,
                        "pull_request": rejected._sync_pr(),
                        "sender": {"login": OWNER},
                    }
                    with self.assertRaisesRegex(
                        approval.ApprovalError,
                        "approval does not match current readiness",
                    ):
                        approval.authorize(
                            event,
                            owner=OWNER,
                            repository=REPOSITORY,
                            actor=OWNER,
                            triggering_actor=OWNER,
                            run_attempt=1,
                            api_url="https://api.github.com",
                            server_url="https://github.com",
                            output=Path(temp) / "stale-approval.json",
                            token=TOKEN,
                            transport=rejected,
                            sleeper=lambda _: None,
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
        ), patch.object(
            approval, "GitHubClient",
            return_value=sync.GitHubClient("https://api.github.com", TOKEN, transport=ReadyTransport()),
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
