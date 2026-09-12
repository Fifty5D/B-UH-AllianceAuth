#!/usr/bin/env python3
"""Bounded validation recovery for the already-published platform v0.6.2.

This module does not publish, rebuild, approve, merge, or deploy a release.  It
validates the one reviewed recovery contract, materializes a disposable future
ledger shape, applies the two-file test-only harness plus its isolated approval
support, and emits canonical evidence for the trusted approval verifier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

try:  # Support package imports and direct workflow execution.
    from . import buh_release, ledger
except ImportError:  # pragma: no cover - exercised by the CLI tests.
    import buh_release  # type: ignore[no-redef]
    import ledger  # type: ignore[no-redef]


SCHEMA_VERSION = 6
ATTESTATION_SCHEMA_VERSION = 3
RECOVERY_ID = "published-platform-v0.6.2-validation-20260909"
WORKFLOW_PATH = ".github/workflows/source-published-release-recovery.yml"
CONTINUATION_WORKFLOW_PATH = (
    ".github/workflows/continue-published-release-recovery.yml"
)
DEFAULT_CONTRACT = Path(__file__).with_name(
    "published-release-recovery-v0.6.2.json"
)
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._/+-]{1,240}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")

HARNESS_PATHS = (
    "tests/deploy/test_request_archive.py",
    "tests/platform/test_coordinated_recovery_rehearsal.py",
)
TEST_SUPPORT_ROOT = ".buh-recovery-test-support"
TEST_SUPPORT_PATHS = (
    ".github/workflows/source-published-release-recovery.yml",
    "ops/release/buh_release.py",
    "ops/release/ledger.py",
    "ops/release/open_sync_pr.py",
    "ops/release/platform_approval.py",
    "ops/release/published-release-recovery-v0.6.2.json",
    "ops/release/recovery_policy.py",
    "ops/release/validation_recovery.py",
    "tests/release/test_platform_approval.py",
)

REQUIRED_CHECK_CONTEXT = "Source test suite / Required source checks"
REQUIRED_CHECK_APP_ID = 15368
REQUIRED_CHECK_APP_SLUG = "github-actions"
RECOVERY_REQUIRED_LANES = (
    "Test configuration",
    "Immutable release ledger",
    "Immutable Moon Tax v0.3.3 recovery artifact",
    "Fast source checks",
    "MariaDB, Redis, Celery, and fake ESI",
    "Legacy schema upgrade and backup restore",
    "Browser tables, actions, and permissions",
    "Required source checks",
)

# Exact tree delta permitted between the v0.6.2 source and the recovery
# activation merge.  Keeping this list in executable code as well as the
# canonical contract prevents a contract edit from silently widening itself.
ACTIVATION_PATHS = (
    ".github/workflows/auto-platform-release.yml",
    ".github/workflows/deploy-approved-platform-release.yml",
    ".github/workflows/deploy-platform-v2.yml",
    ".github/workflows/reusable-source-tests.yml",
    ".github/workflows/source-published-release-recovery.yml",
    "changes/recovery-rehearsal-snapshot-isolation.toml",
    "ops/release/README.md",
    "ops/release/platform_approval.py",
    "ops/release/published-release-recovery-v0.6.2.json",
    "ops/release/validation_recovery.py",
    "tests/deploy/test_request_archive.py",
    "tests/platform/test_configuration.py",
    "tests/platform/test_coordinated_recovery_rehearsal.py",
    "tests/platform/test_release_workflow_execution.py",
    "tests/release/test_platform_approval.py",
    "tests/release/test_validation_recovery.py",
)

# Exact tree delta permitted for the one follow-up after activation PR #53.
# This list is intentionally separate from ACTIVATION_PATHS: the original merge
# remains immutable evidence and cannot be reinterpreted as the repair harness.
CONTINUATION_PATHS = (
    ".github/workflows/reusable-source-tests.yml",
    ".github/workflows/source-published-release-recovery.yml",
    "changes/recovery-rehearsal-snapshot-isolation.toml",
    "ops/release/README.md",
    "ops/release/platform_approval.py",
    "ops/release/published-release-recovery-v0.6.2.json",
    "ops/release/validation_recovery.py",
    "tests/platform/test_configuration.py",
    "tests/platform/test_coordinated_recovery_rehearsal.py",
    "tests/platform/test_release_workflow_execution.py",
    "tests/release/test_platform_approval.py",
    "tests/release/test_validation_recovery.py",
)

# Exact tree delta permitted for the one digest-handoff repair after PR #54.
# PR #54 remains immutable evidence; this separately bounded merge may change
# only the workflow boundary, its approval/continuation contracts, tests, docs,
# and the one new change fragment.
REPAIR_PATHS = (
    ".github/workflows/reusable-source-tests.yml",
    ".github/workflows/source-published-release-recovery.yml",
    "changes/recovery-artifact-digest-handoff.toml",
    "ops/release/README.md",
    "ops/release/platform_approval.py",
    "ops/release/published-release-recovery-v0.6.2.json",
    "ops/release/validation_recovery.py",
    "tests/platform/test_configuration.py",
    "tests/platform/test_coordinated_recovery_rehearsal.py",
    "tests/platform/test_release_workflow_execution.py",
    "tests/release/test_platform_approval.py",
    "tests/release/test_validation_recovery.py",
)

# Exact tree delta permitted for the publication-continuation repair after
# PR #55. The already-tested harness remains the immutable PR #55 merge; this
# follow-up may only repair the GitHub evidence handoff and its focused tests.
PUBLICATION_REPAIR_PATHS = (
    ".github/workflows/continue-published-release-recovery.yml",
    ".github/workflows/reusable-source-tests.yml",
    "changes/recovery-check-publication-continuation.toml",
    "ops/release/README.md",
    "ops/release/platform_approval.py",
    "ops/release/published-release-recovery-v0.6.2.json",
    "ops/release/validation_recovery.py",
    "tests/platform/test_configuration.py",
    "tests/platform/test_coordinated_recovery_rehearsal.py",
    "tests/platform/test_release_workflow_execution.py",
    "tests/release/fixtures/github-check-run-103395196156-graphql.json",
    "tests/release/fixtures/github-check-run-103395196156.json",
    "tests/release/fixtures/github-main-required-checks.json",
    "tests/release/test_platform_approval.py",
    "tests/release/test_validation_recovery.py",
)

# Exact tree delta permitted for the repair that separates the mutable,
# validated synchronization head from the immutable v0.6.2 deployment target.
# The merge of this PR is the only main commit that may be merged into the
# synchronization branch by the bounded update path below.
SYNC_REPAIR_PATHS = (
    ".github/workflows/continue-synchronized-release-recovery.yml",
    ".github/workflows/deploy-approved-platform-release.yml",
    ".github/workflows/deploy-platform-v2.yml",
    ".github/workflows/reusable-source-tests.yml",
    "changes/sync-head-release-identity.toml",
    "ops/release/README.md",
    "ops/release/platform_approval.py",
    "ops/release/published-release-recovery-v0.6.2.json",
    "ops/release/validation_recovery.py",
    "tests/platform/test_configuration.py",
    "tests/platform/test_coordinated_recovery_rehearsal.py",
    "tests/platform/test_release_workflow_execution.py",
    "tests/release/fixtures/github-pr-52-behind.json",
    "tests/release/fixtures/github-required-check-history-6074b965.json",
    "tests/release/test_platform_approval.py",
    "tests/release/test_validation_recovery.py",
)

# Exact tree delta permitted for the check-association repair after PR #57's
# reviewed merge and the first synchronization-branch update. This second
# bounded repair preserves the already-updated sync head and may only change
# the historical-check association guard, its continuation workflow, fixtures,
# focused tests, documentation, and this one change fragment.
CHECK_ASSOCIATION_REPAIR_PATHS = (
    ".github/workflows/continue-synchronized-release-recovery.yml",
    ".github/workflows/reusable-source-tests.yml",
    "changes/historical-check-pr-association.toml",
    "ops/release/README.md",
    "ops/release/platform_approval.py",
    "ops/release/published-release-recovery-v0.6.2.json",
    "ops/release/validation_recovery.py",
    "tests/platform/test_configuration.py",
    "tests/platform/test_coordinated_recovery_rehearsal.py",
    "tests/platform/test_release_workflow_execution.py",
    "tests/release/fixtures/github-check-run-102298823162.json",
    "tests/release/fixtures/github-check-run-103395196156-graphql.json",
    "tests/release/fixtures/github-check-run-103395196156.json",
    "tests/release/fixtures/github-required-check-history-6074b965.json",
    "tests/release/test_platform_approval.py",
    "tests/release/test_validation_recovery.py",
)


class ValidationRecoveryError(ValueError):
    """The bounded recovery contract or claimed identity is invalid."""


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _safe_path(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or SAFE_PATH_RE.fullmatch(value) is None:
        raise ValidationRecoveryError(f"{context} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(
        part in {"", ".", "..", ".git"} for part in path.parts
    ):
        raise ValidationRecoveryError(f"{context} is invalid")
    return value


def _positive_int(value: Any, *, context: str) -> int:
    if type(value) is not int or not 1 <= value <= 10**20:
        raise ValidationRecoveryError(f"{context} is invalid")
    return value


def _commit(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or COMMIT_RE.fullmatch(value) is None:
        raise ValidationRecoveryError(f"{context} is invalid")
    return value


def _sha256(value: Any, *, context: str, prefixed: bool = False) -> str:
    pattern = DIGEST_RE if prefixed else SHA256_RE
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValidationRecoveryError(f"{context} is invalid")
    return value


def load_contract(path: Path = DEFAULT_CONTRACT) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationRecoveryError("Validation recovery contract is unavailable") from exc
    if not raw or len(raw) > 64 * 1024:
        raise ValidationRecoveryError("Validation recovery contract has an unsafe size")
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationRecoveryError("Validation recovery contract is unreadable") from exc
    if raw != canonical_json_bytes(value):
        raise ValidationRecoveryError("Validation recovery contract is not canonical")
    if not isinstance(value, dict) or set(value) != {
        "activation",
        "check_association_repair",
        "continuation",
        "failed_publication",
        "failed_validation",
        "feature",
        "main_validation",
        "partial_publication",
        "platform_version",
        "preflight",
        "publication_repair",
        "repair",
        "required_check",
        "recovery_id",
        "release",
        "repository",
        "schema_version",
        "sync",
        "sync_repair",
        "historical_readiness",
    }:
        raise ValidationRecoveryError("Validation recovery contract has invalid fields")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("recovery_id") != RECOVERY_ID
        or value.get("repository") != "Fifty5D/B-UH-AllianceAuth"
        or value.get("platform_version") != "0.6.2"
    ):
        raise ValidationRecoveryError("Validation recovery contract identity changed")

    activation = value.get("activation")
    if not isinstance(activation, dict) or set(activation) != {
        "allowed_paths",
        "base_commit",
        "commit",
        "feature_head",
        "pull_request",
        "tree",
        "validation",
    }:
        raise ValidationRecoveryError("Recovery activation contract is invalid")
    activation_validation = activation.get("validation")
    if not isinstance(activation_validation, dict) or set(activation_validation) != {
        "run_attempt",
        "run_id",
    }:
        raise ValidationRecoveryError("Recovery activation validation is invalid")
    if (
        _positive_int(activation.get("pull_request"), context="Activation PR") != 53
        or _commit(activation.get("base_commit"), context="Activation base")
        != "4f98e7cb559ee1a9b269ea1f94938678d2dac4df"
        or _commit(activation.get("commit"), context="Activation commit")
        != "e0bd37fafcedee3135aa4c4d6bdfc7778d032d48"
        or _commit(activation.get("feature_head"), context="Activation feature head")
        != "a8aaf10e03c35a5510f0a7e03a92d4f19e8be0e8"
        or _commit(activation.get("tree"), context="Activation tree")
        != "1876038432e135721b4c3cd8117f37a0bcf013b6"
        or activation.get("allowed_paths") != list(ACTIVATION_PATHS)
        or activation_validation != {"run_attempt": 1, "run_id": 34427799821}
    ):
        raise ValidationRecoveryError("Recovery activation scope changed")

    continuation = value.get("continuation")
    if not isinstance(continuation, dict) or set(continuation) != {
        "allowed_paths",
        "base_commit",
        "commit",
        "feature_head",
        "pull_request",
        "tree",
        "validation",
    }:
        raise ValidationRecoveryError("Recovery continuation contract is invalid")
    continuation_validation = continuation.get("validation")
    if not isinstance(continuation_validation, dict) or set(
        continuation_validation
    ) != {"run_attempt", "run_id"}:
        raise ValidationRecoveryError("Recovery continuation validation is invalid")
    if (
        _positive_int(continuation.get("pull_request"), context="Continuation PR")
        != 54
        or _commit(continuation.get("base_commit"), context="Continuation base")
        != activation["commit"]
        or _commit(continuation.get("commit"), context="Continuation commit")
        != "57e0e29042bab3a989c80e5473ad552ec5b4505b"
        or _commit(
            continuation.get("feature_head"), context="Continuation feature head"
        )
        != "a743e222546371f386a629459ab8e7b209684905"
        or _commit(continuation.get("tree"), context="Continuation tree")
        != "4fdb46bcbd29081d7a04a7cd41bd7e59f5210c50"
        or continuation.get("allowed_paths") != list(CONTINUATION_PATHS)
        or continuation_validation != {"run_attempt": 1, "run_id": 34440024442}
    ):
        raise ValidationRecoveryError("Recovery continuation scope changed")

    repair = value.get("repair")
    if not isinstance(repair, dict) or set(repair) != {
        "allowed_paths",
        "base_commit",
        "harness_paths",
        "pull_request",
        "test_support_paths",
        "test_support_root",
    }:
        raise ValidationRecoveryError("Recovery digest repair contract is invalid")
    if (
        _positive_int(repair.get("pull_request"), context="Digest repair PR") != 55
        or _commit(repair.get("base_commit"), context="Digest repair base")
        != continuation["commit"]
        or repair.get("allowed_paths") != list(REPAIR_PATHS)
        or repair.get("harness_paths") != list(HARNESS_PATHS)
        or repair.get("test_support_paths") != list(TEST_SUPPORT_PATHS)
        or repair.get("test_support_root") != TEST_SUPPORT_ROOT
    ):
        raise ValidationRecoveryError("Recovery digest repair scope changed")

    publication_repair = value.get("publication_repair")
    if not isinstance(publication_repair, dict) or set(publication_repair) != {
        "allowed_paths",
        "base_commit",
        "pull_request",
    }:
        raise ValidationRecoveryError("Recovery publication repair contract is invalid")
    if (
        _positive_int(
            publication_repair.get("pull_request"), context="Publication repair PR"
        )
        != 56
        or _commit(
            publication_repair.get("base_commit"), context="Publication repair base"
        )
        != "f064694cca7e9d1147a87b880636a94f5c5cbe94"
        or publication_repair.get("allowed_paths")
        != list(PUBLICATION_REPAIR_PATHS)
    ):
        raise ValidationRecoveryError("Recovery publication repair scope changed")

    sync_repair = value.get("sync_repair")
    if not isinstance(sync_repair, dict) or set(sync_repair) != {
        "allowed_paths",
        "base_commit",
        "pull_request",
    }:
        raise ValidationRecoveryError("Synchronization-head repair contract is invalid")
    if (
        _positive_int(sync_repair.get("pull_request"), context="Sync repair PR")
        != 57
        or _commit(sync_repair.get("base_commit"), context="Sync repair base")
        != "9f50567ae85a0bc9219c2c4f03e55dbb431e4cfb"
        or sync_repair.get("allowed_paths") != list(SYNC_REPAIR_PATHS)
    ):
        raise ValidationRecoveryError("Synchronization-head repair scope changed")

    association_repair = value.get("check_association_repair")
    if not isinstance(association_repair, dict) or set(association_repair) != {
        "allowed_paths",
        "base_commit",
        "pull_request",
    }:
        raise ValidationRecoveryError("Check-association repair contract is invalid")
    if (
        _positive_int(
            association_repair.get("pull_request"),
            context="Check-association repair PR",
        )
        != 59
        or _commit(
            association_repair.get("base_commit"),
            context="Check-association repair base",
        )
        != "8795ebf2e82b0bc204047b0b7834ee23cd24edb6"
        or association_repair.get("allowed_paths")
        != list(CHECK_ASSOCIATION_REPAIR_PATHS)
    ):
        raise ValidationRecoveryError("Check-association repair scope changed")

    historical_readiness = value.get("historical_readiness")
    if not isinstance(historical_readiness, dict) or historical_readiness != {
        "approval_nonce": (
            "ac957ad0bea53e3d3188bcd9f024cb8fafde94872eb1b10f8d28790f3a669982"
        ),
        "comment_id": 5641670561,
        "created_at": "2026-09-11T23:09:03Z",
        "schema_version": 7,
        "sync_base_commit": "9f50567ae85a0bc9219c2c4f03e55dbb431e4cfb",
        "sync_head_commit": "6074b965cbd2e6ab2630cd539ee455b8d419aef6",
    }:
        raise ValidationRecoveryError("Historical recovery readiness identity changed")

    failed_validation = value.get("failed_validation")
    if not isinstance(failed_validation, dict) or set(failed_validation) != {
        "artifact",
        "jobs",
        "run_attempt",
        "run_id",
    }:
        raise ValidationRecoveryError("Failed recovery validation evidence is invalid")
    failed_artifact = failed_validation.get("artifact")
    if (
        _positive_int(failed_validation.get("run_id"), context="Failed recovery run ID")
        != 34428188769
        or _positive_int(
            failed_validation.get("run_attempt"),
            context="Failed recovery run attempt",
        )
        != 1
        or failed_artifact
        != {
            "digest": "sha256:a7f399562d0c4ddc4be025b17acc4a448ec518561537cffcb7be2d61016fbd4b",
            "id": 10133470248,
            "name": "source-fast-failure-34428188769-1",
        }
    ):
        raise ValidationRecoveryError("Failed recovery validation identity changed")
    failed_jobs = failed_validation.get("jobs")
    if not isinstance(failed_jobs, list) or len(failed_jobs) != 11:
        raise ValidationRecoveryError("Failed recovery validation job set is invalid")
    for item in failed_jobs:
        if (
            not isinstance(item, dict)
            or set(item) != {"conclusion", "id", "name"}
            or item.get("conclusion") not in {"failure", "skipped", "success"}
            or not isinstance(item.get("name"), str)
            or not item["name"]
        ):
            raise ValidationRecoveryError("Failed recovery validation job is invalid")
        _positive_int(item.get("id"), context="Failed recovery job ID")
    if {item["id"] for item in failed_jobs} != {
        102717801188,
        102717830025,
        102717830061,
        102717830063,
        102717855709,
        102717855713,
        102717855748,
        102717855764,
        102718509763,
        102718527684,
        102718528156,
    }:
        raise ValidationRecoveryError("Failed recovery validation jobs changed")
    if [item["id"] for item in failed_jobs if item["conclusion"] == "failure"] != [
        102717855709,
        102718509763,
    ]:
        raise ValidationRecoveryError("Failed recovery validation failure set changed")

    failed_publication = value.get("failed_publication")
    if not isinstance(failed_publication, dict) or set(failed_publication) != {
        "artifact",
        "jobs",
        "run_attempt",
        "run_id",
    }:
        raise ValidationRecoveryError("Failed recovery publication evidence is invalid")
    failed_publication_artifact = failed_publication.get("artifact")
    if (
        _positive_int(
            failed_publication.get("run_id"),
            context="Failed publication run ID",
        )
        != 34440488685
        or _positive_int(
            failed_publication.get("run_attempt"),
            context="Failed publication run attempt",
        )
        != 1
        or failed_publication_artifact
        != {
            "digest": "sha256:b04387d5eb10ae9a4e9abf0eaf87d87e264a8b7aeb5937fac4ad9dbc5168143f",
            "id": 10137840766,
            "name": (
                "platform-validation-recovery-v0.6.2-6074b965cbd2-"
                "34440488685-1"
            ),
        }
    ):
        raise ValidationRecoveryError("Failed recovery publication identity changed")
    failed_publication_jobs = failed_publication.get("jobs")
    if not isinstance(failed_publication_jobs, list) or len(
        failed_publication_jobs
    ) != 11:
        raise ValidationRecoveryError("Failed recovery publication job set is invalid")
    for item in failed_publication_jobs:
        if (
            not isinstance(item, dict)
            or set(item) != {"conclusion", "id", "name"}
            or item.get("conclusion") not in {"failure", "success"}
            or not isinstance(item.get("name"), str)
            or not item["name"]
        ):
            raise ValidationRecoveryError("Failed recovery publication job is invalid")
        _positive_int(item.get("id"), context="Failed publication job ID")
    if {item["id"] for item in failed_publication_jobs} != {
        102754324829,
        102754356788,
        102754356792,
        102754356793,
        102754378571,
        102754378637,
        102754378658,
        102754378963,
        102755185034,
        102755200516,
        102755231308,
    } or [
        item["id"]
        for item in failed_publication_jobs
        if item["conclusion"] == "failure"
    ] != [102755231308]:
        raise ValidationRecoveryError("Failed recovery publication jobs changed")

    partial_publication = value.get("partial_publication")
    if not isinstance(partial_publication, dict) or set(partial_publication) != {
        "artifact",
        "attestation_sha256",
        "jobs",
        "required_check",
        "run_attempt",
        "run_id",
    }:
        raise ValidationRecoveryError("Partial recovery publication evidence is invalid")
    partial_artifact = partial_publication.get("artifact")
    if (
        _positive_int(
            partial_publication.get("run_id"), context="Partial publication run ID"
        )
        != 34638993007
        or _positive_int(
            partial_publication.get("run_attempt"),
            context="Partial publication run attempt",
        )
        != 1
        or partial_artifact
        != {
            "digest": "sha256:2890ce18bd66892972c732a92fbdd82aec22c899a752353375f5e91a8335c37b",
            "id": 10279641606,
            "name": (
                "platform-validation-recovery-v0.6.2-6074b965cbd2-"
                "34638993007-1"
            ),
        }
        or _sha256(
            partial_publication.get("attestation_sha256"),
            context="Partial publication attestation digest",
            prefixed=True,
        )
        != "sha256:109f96e5d81de4a2f97de239ba71170dde6a0eb65c1240eb48047550825d594d"
    ):
        raise ValidationRecoveryError("Partial recovery publication identity changed")
    partial_jobs = partial_publication.get("jobs")
    if not isinstance(partial_jobs, list) or len(partial_jobs) != 11:
        raise ValidationRecoveryError("Partial recovery publication job set is invalid")
    for item in partial_jobs:
        if (
            not isinstance(item, dict)
            or set(item) != {"conclusion", "id", "name"}
            or item.get("conclusion") not in {"failure", "success"}
            or not isinstance(item.get("name"), str)
            or not item["name"]
        ):
            raise ValidationRecoveryError("Partial recovery publication job is invalid")
        _positive_int(item.get("id"), context="Partial publication job ID")
    if {item["id"] for item in partial_jobs} != {
        103393779917,
        103393822048,
        103393822100,
        103393822134,
        103393847842,
        103393847860,
        103393847866,
        103393847901,
        103394825002,
        103394856094,
        103394892961,
    } or [item["id"] for item in partial_jobs if item["conclusion"] == "failure"] != [
        103394892961
    ]:
        raise ValidationRecoveryError("Partial recovery publication jobs changed")
    partial_check = partial_publication.get("required_check")
    if partial_check != {
        "app_id": REQUIRED_CHECK_APP_ID,
        "app_slug": REQUIRED_CHECK_APP_SLUG,
        "binding_digest": (
            "sha256:544fe3a61336f5e9136180588a2c8ca05afefb3d9c7c2adb5b44a7387f8bee25"
        ),
        "check_run_id": 103395196156,
        "check_run_node_id": "CR_kwDOUIayms8AAAAYEtV8_A",
        "check_suite_id": 92911719432,
        "completed_at": "2026-09-11T19:32:53Z",
        "context": REQUIRED_CHECK_CONTEXT,
        "details_url": (
            "https://github.com/Fifty5D/B-UH-AllianceAuth/runs/103395196156"
        ),
        "external_id": (
            "published-platform-v0.6.2-validation-20260909:"
            "544fe3a61336f5e9136180588a2c8ca05afefb3d9c7c2adb5b44a7387f8bee25"
        ),
        "head_sha": "6074b965cbd2e6ab2630cd539ee455b8d419aef6",
        "historical_check_run_id": 102298823162,
        "required_pull_request": 52,
    }:
        raise ValidationRecoveryError("Partial recovery required-check identity changed")

    feature = value.get("feature")
    if not isinstance(feature, dict) or set(feature) != {
        "head_commit",
        "pull_request",
    }:
        raise ValidationRecoveryError("Recovery feature identity is invalid")
    if (
        _positive_int(feature.get("pull_request"), context="Feature PR") != 51
        or _commit(feature.get("head_commit"), context="Feature head")
        != "7e945de03a75e8d7ec75930f7e60fe402abfcf48"
    ):
        raise ValidationRecoveryError("Recovery feature identity changed")

    main_validation = value.get("main_validation")
    if not isinstance(main_validation, dict) or set(main_validation) != {
        "run_attempt",
        "run_id",
    }:
        raise ValidationRecoveryError("Recovery main validation is invalid")
    if (
        _positive_int(main_validation.get("run_id"), context="Main run ID")
        != 34297290478
        or _positive_int(
            main_validation.get("run_attempt"), context="Main run attempt"
        )
        != 1
    ):
        raise ValidationRecoveryError("Recovery main validation identity changed")

    release = value.get("release")
    if not isinstance(release, dict) or set(release) != {
        "commit",
        "manifest_sha256",
        "ref",
        "source_commit",
        "source_tree",
        "tree",
    }:
        raise ValidationRecoveryError("Recovery release identity is invalid")
    expected_release = {
        "commit": "6074b965cbd2e6ab2630cd539ee455b8d419aef6",
        "manifest_sha256": (
            "c9cf79d34c8b3f90556e405f338ee4c1e7818d9c686f0462eec7dca201b784f4"
        ),
        "ref": "release/platform-v0.6.2",
        "source_commit": "4f98e7cb559ee1a9b269ea1f94938678d2dac4df",
        "source_tree": "83ab4b808ce44a0de99fef9d255aaceb10c89f21",
        "tree": "cfcd1f21903d3609aef0bbd7b61950be5fef2aa5",
    }
    if release != expected_release:
        raise ValidationRecoveryError("Recovery release identity changed")

    sync = value.get("sync")
    if not isinstance(sync, dict) or set(sync) != {
        "branch",
        "initial_head_commit",
        "prior_head_commit",
        "prior_head_tree",
        "pull_request",
    }:
        raise ValidationRecoveryError("Recovery synchronization identity is invalid")
    if sync != {
        "branch": "sync/platform-v0.6.2",
        "initial_head_commit": "6074b965cbd2e6ab2630cd539ee455b8d419aef6",
        "prior_head_commit": "4b4a6f2f2e6eeb7bd85010537fb76ac2c2e44ef6",
        "prior_head_tree": "94094d88bf64f9f65c58e0cc8d724eb8364a9d62",
        "pull_request": 52,
    }:
        raise ValidationRecoveryError("Recovery synchronization identity changed")

    preflight = value.get("preflight")
    if not isinstance(preflight, dict) or set(preflight) != {
        "artifact_digest",
        "artifact_id",
        "artifact_name",
        "expires_at",
        "failed_job",
        "jobs",
        "passed_job",
        "run_attempt",
        "run_id",
    }:
        raise ValidationRecoveryError("Recovery preflight identity is invalid")
    if (
        _positive_int(preflight.get("run_id"), context="Preflight run ID")
        != 34297562022
        or _positive_int(
            preflight.get("run_attempt"), context="Preflight run attempt"
        )
        != 1
        or _positive_int(
            preflight.get("artifact_id"), context="Preflight artifact ID"
        )
        != 10083806725
        or preflight.get("artifact_name")
        != "platform-v2-preflight-34297562022-1"
        or preflight.get("expires_at") != "2026-09-16T01:07:13Z"
        or _sha256(
            preflight.get("artifact_digest"),
            context="Preflight artifact digest",
            prefixed=True,
        )
        != "sha256:d527be28e5f79972835cd2d12c92e6ce98f4c9f77f081e872df76908c6ca0510"
    ):
        raise ValidationRecoveryError("Recovery preflight evidence changed")
    expected_passed = {
        "id": 102298090053,
        "name": "Run production no-change preflight / Guarded Platform v2 operation",
    }
    expected_failed = {
        "id": 102298334241,
        "name": "Publish ChatGPT approval evidence",
    }
    if preflight.get("passed_job") != expected_passed:
        raise ValidationRecoveryError("Recovery passed-job identity changed")
    if preflight.get("failed_job") != expected_failed:
        raise ValidationRecoveryError("Recovery failed-job identity changed")
    jobs = preflight.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != 9:
        raise ValidationRecoveryError("Recovery release-run job set is invalid")
    for item in jobs:
        if (
            not isinstance(item, dict)
            or set(item) != {"conclusion", "id", "name"}
            or item.get("conclusion") not in {"success", "failure"}
            or not isinstance(item.get("name"), str)
            or not item["name"]
        ):
            raise ValidationRecoveryError("Recovery release-run job is invalid")
        _positive_int(item.get("id"), context="Release-run job ID")
    if [item for item in jobs if item["conclusion"] == "failure"] != [
        {**expected_failed, "conclusion": "failure"}
    ]:
        raise ValidationRecoveryError("Recovery release run has the wrong failure set")
    if {item["id"] for item in jobs} != {
        102297369628,
        102297426544,
        102297471844,
        102297806545,
        102297826409,
        102297905804,
        102298055486,
        102298090053,
        102298334241,
    }:
        raise ValidationRecoveryError("Recovery release-run jobs changed")

    required_check = value.get("required_check")
    if not isinstance(required_check, dict) or set(required_check) != {
        "app_id",
        "app_slug",
        "branch",
        "context",
        "historical_failure",
        "required_lanes",
    }:
        raise ValidationRecoveryError("Recovery required-check contract is invalid")
    historical = required_check.get("historical_failure")
    if not isinstance(historical, dict) or set(historical) != {
        "check_run_id",
        "conclusion",
        "details_url",
        "run_attempt",
        "workflow_run_id",
    }:
        raise ValidationRecoveryError("Recovery historical check is invalid")
    if (
        required_check.get("app_id") != REQUIRED_CHECK_APP_ID
        or required_check.get("app_slug") != REQUIRED_CHECK_APP_SLUG
        or required_check.get("branch") != "main"
        or required_check.get("context") != REQUIRED_CHECK_CONTEXT
        or required_check.get("required_lanes") != list(RECOVERY_REQUIRED_LANES)
        or historical
        != {
            "check_run_id": 102298823162,
            "conclusion": "failure",
            "details_url": (
                "https://github.com/Fifty5D/B-UH-AllianceAuth/actions/runs/"
                "34297801831/job/102298823162"
            ),
            "run_attempt": 1,
            "workflow_run_id": 34297801831,
        }
    ):
        raise ValidationRecoveryError("Recovery required-check identity changed")
    return value


def _git(
    root: Path,
    arguments: Sequence[str],
    *,
    operation: str,
    environment: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            env=dict(environment) if environment is not None else None,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, UnicodeError) as exc:
        raise ValidationRecoveryError(f"Git {operation} could not run") from exc
    if check and completed.returncode != 0:
        raise ValidationRecoveryError(
            f"Git {operation} failed with exit code {completed.returncode}"
        )
    return completed


def _git_bytes(root: Path, arguments: Sequence[str], *, operation: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise ValidationRecoveryError(f"Git {operation} could not run") from exc
    if completed.returncode != 0:
        raise ValidationRecoveryError(
            f"Git {operation} failed with exit code {completed.returncode}"
        )
    return completed.stdout


def _resolve(root: Path, revision: str) -> str:
    value = _git(
        root,
        ["rev-parse", "--verify", f"{revision}^{{commit}}"],
        operation="commit resolution",
    ).stdout.strip()
    return _commit(value, context="Resolved commit")


def _tree(root: Path, commit: str) -> str:
    value = _git(
        root,
        ["rev-parse", f"{commit}^{{tree}}"],
        operation="tree resolution",
    ).stdout.strip()
    if COMMIT_RE.fullmatch(value) is None:
        raise ValidationRecoveryError("Resolved tree identity is invalid")
    return value


def _parents(root: Path, commit: str) -> list[str]:
    line = _git(
        root,
        ["show", "-s", "--format=%P", commit],
        operation="parent inspection",
    ).stdout.strip()
    parents = line.split() if line else []
    if any(COMMIT_RE.fullmatch(parent) is None for parent in parents):
        raise ValidationRecoveryError("Commit parent identity is invalid")
    return parents


def _changed_paths(root: Path, base: str, commit: str) -> tuple[str, ...]:
    raw = _git_bytes(
        root,
        ["diff", "--name-only", "-z", base, commit, "--"],
        operation="activation path inspection",
    )
    try:
        values = tuple(
            sorted(
                _safe_path(item.decode("utf-8"), context="Changed path")
                for item in raw.split(b"\0")
                if item
            )
        )
    except UnicodeError as exc:
        raise ValidationRecoveryError("Activation path is not UTF-8") from exc
    if len(values) != len(set(values)):
        raise ValidationRecoveryError("Activation path list contains a duplicate")
    return values


def _blob(root: Path, commit: str, path: str) -> tuple[str, bytes]:
    safe = _safe_path(path, context="Git blob path")
    record = _git_bytes(
        root,
        ["ls-tree", "-z", commit, "--", safe],
        operation="blob identity inspection",
    )
    entries = [item for item in record.split(b"\0") if item]
    if len(entries) != 1:
        raise ValidationRecoveryError(f"Recovery path is not one Git entry: {safe}")
    try:
        header, returned = entries[0].split(b"\t", 1)
        mode, object_type, object_id = header.decode("ascii").split()
        returned_path = returned.decode("utf-8")
    except (UnicodeError, ValueError) as exc:
        raise ValidationRecoveryError("Git returned malformed blob identity") from exc
    if (
        mode != "100644"
        or object_type != "blob"
        or COMMIT_RE.fullmatch(object_id) is None
        or returned_path != safe
    ):
        raise ValidationRecoveryError(f"Recovery path is not a 100644 blob: {safe}")
    content = _git_bytes(
        root,
        ["cat-file", "blob", f"{commit}:{safe}"],
        operation="blob read",
    )
    return object_id, content


def _verify_release_identity(root: Path, contract: Mapping[str, Any]) -> None:
    release = contract["release"]
    source = _resolve(root, release["source_commit"])
    published = _resolve(root, release["commit"])
    if _tree(root, source) != release["source_tree"]:
        raise ValidationRecoveryError("Reviewed release source tree changed")
    if _tree(root, published) != release["tree"]:
        raise ValidationRecoveryError("Published release tree changed")
    if _parents(root, published) != [source]:
        raise ValidationRecoveryError("Published release ancestry changed")
    manifest_path = f"releases/platform/v{contract['platform_version']}/RELEASE.json"
    _, manifest_bytes = _blob(root, published, manifest_path)
    if hashlib.sha256(manifest_bytes).hexdigest() != release["manifest_sha256"]:
        raise ValidationRecoveryError("Published release manifest changed")
    try:
        manifest = json.loads(manifest_bytes.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationRecoveryError("Published release manifest is unreadable") from exc
    if (
        manifest.get("platform_version") != contract["platform_version"]
        or manifest.get("source_commit") != source
    ):
        raise ValidationRecoveryError("Published release manifest identity changed")


def _blob_records(
    root: Path, commit: str, paths: Sequence[str]
) -> list[dict[str, str]]:
    records = []
    for path in paths:
        object_id, content = _blob(root, commit, path)
        records.append(
            {
                "git_blob_sha": object_id,
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return records


def verify_activation(
    root: Path, *, contract: Mapping[str, Any]
) -> dict[str, Any]:
    """Verify the already-merged PR #53 activation as immutable evidence."""

    root = root.resolve()
    activation = contract["activation"]
    base = _resolve(root, activation["base_commit"])
    commit = _resolve(root, activation["commit"])
    feature_head = _resolve(root, activation["feature_head"])
    if (
        _parents(root, commit) != [base, feature_head]
        or _tree(root, commit) != activation["tree"]
        or _tree(root, feature_head) != activation["tree"]
        or _changed_paths(root, base, feature_head) != ACTIVATION_PATHS
        or _changed_paths(root, base, commit) != ACTIVATION_PATHS
    ):
        raise ValidationRecoveryError("Original recovery activation identity changed")
    return {
        "activation_commit": commit,
        "activation_tree": activation["tree"],
        "base_commit": base,
        "feature_head": feature_head,
        "paths": _blob_records(root, commit, ACTIVATION_PATHS),
        "pull_request": activation["pull_request"],
    }


def verify_continuation(root: Path, *, contract: Mapping[str, Any]) -> dict[str, Any]:
    """Verify already-merged PR #54 as immutable continuation evidence."""

    root = root.resolve()
    activation = verify_activation(root, contract=contract)
    continuation = contract["continuation"]
    base = activation["activation_commit"]
    commit = _resolve(root, continuation["commit"])
    feature_head = _resolve(root, continuation["feature_head"])
    if continuation["base_commit"] != base:
        raise ValidationRecoveryError("Recovery continuation base changed")
    if (
        _parents(root, commit) != [base, feature_head]
        or _tree(root, commit) != continuation["tree"]
        or _tree(root, feature_head) != continuation["tree"]
        or _changed_paths(root, base, feature_head) != CONTINUATION_PATHS
        or _changed_paths(root, base, commit) != CONTINUATION_PATHS
    ):
        raise ValidationRecoveryError("Recovery continuation identity changed")
    return {
        "base_commit": base,
        "continuation_commit": commit,
        "continuation_tree": continuation["tree"],
        "feature_head": feature_head,
        "paths": _blob_records(root, commit, CONTINUATION_PATHS),
        "pull_request": continuation["pull_request"],
    }


def validate_repair(
    root: Path,
    current_commit: str,
    *,
    event_name: str,
    pull_request: int | None,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the exact digest-repair branch or its one merge commit."""

    root = root.resolve()
    current = _resolve(root, current_commit)
    continuation = verify_continuation(root, contract=contract)
    repair = contract["repair"]
    base = continuation["continuation_commit"]
    release = contract["release"]["commit"]
    _verify_release_identity(root, contract)
    if repair["base_commit"] != base:
        raise ValidationRecoveryError("Recovery digest repair base changed")
    if _git(
        root,
        ["merge-base", "--is-ancestor", base, current],
        operation="digest repair ancestry inspection",
        check=False,
    ).returncode != 0:
        raise ValidationRecoveryError("Digest repair is not based on PR #54")
    if _git(
        root,
        ["merge-base", "--is-ancestor", release, current],
        operation="unsynchronized release inspection",
        check=False,
    ).returncode == 0:
        raise ValidationRecoveryError("Digest repair unexpectedly contains the release ledger")

    if event_name == "pull_request":
        if pull_request != repair["pull_request"]:
            raise ValidationRecoveryError("Recovery is restricted to digest repair PR #55")
        feature_head = current
        repair_commit = None
    elif event_name in {"push", "workflow_dispatch"}:
        parents = _parents(root, current)
        if len(parents) != 2 or parents[0] != base:
            raise ValidationRecoveryError(
                "Recovery digest repair is not a direct two-parent merge onto PR #54"
            )
        feature_head = parents[1]
        if _tree(root, feature_head) != _tree(root, current):
            raise ValidationRecoveryError(
                "Digest repair merge tree differs from its reviewed head"
            )
        repair_commit = current
    else:
        raise ValidationRecoveryError("Event cannot continue published-release recovery")

    if _git(
        root,
        ["merge-base", "--is-ancestor", base, feature_head],
        operation="digest repair feature ancestry inspection",
        check=False,
    ).returncode != 0:
        raise ValidationRecoveryError("Digest repair feature head has invalid ancestry")
    changed = _changed_paths(root, base, feature_head)
    if changed != REPAIR_PATHS:
        raise ValidationRecoveryError("Digest repair tree delta is outside the reviewed scope")
    if _changed_paths(root, base, current) != REPAIR_PATHS:
        raise ValidationRecoveryError("Current digest repair tree delta changed")
    return {
        "base_commit": base,
        "feature_head": feature_head,
        "paths": _blob_records(root, current, REPAIR_PATHS),
        "pull_request": repair["pull_request"],
        "repair_commit": repair_commit,
        "repair_tree": _tree(root, current),
    }


def validate_publication_repair(
    root: Path,
    current_commit: str,
    *,
    event_name: str,
    pull_request: int | None,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the exact check-publication repair branch or merge commit."""

    root = root.resolve()
    current = _resolve(root, current_commit)
    publication = contract["publication_repair"]
    base = _resolve(root, publication["base_commit"])
    release = contract["release"]["commit"]
    validate_repair(
        root,
        base,
        event_name="workflow_dispatch",
        pull_request=None,
        contract=contract,
    )
    _verify_release_identity(root, contract)
    if _git(
        root,
        ["merge-base", "--is-ancestor", base, current],
        operation="publication repair ancestry inspection",
        check=False,
    ).returncode != 0:
        raise ValidationRecoveryError("Publication repair is not based on PR #55")
    if _git(
        root,
        ["merge-base", "--is-ancestor", release, current],
        operation="publication repair release inspection",
        check=False,
    ).returncode == 0:
        raise ValidationRecoveryError(
            "Publication repair unexpectedly contains the release ledger"
        )

    if event_name == "pull_request":
        if pull_request != publication["pull_request"]:
            raise ValidationRecoveryError("Recovery is restricted to publication PR #56")
        feature_head = current
        publication_commit = None
    elif event_name in {"push", "workflow_dispatch"}:
        parents = _parents(root, current)
        if len(parents) != 2 or parents[0] != base:
            raise ValidationRecoveryError(
                "Recovery publication repair is not a direct two-parent merge onto PR #55"
            )
        feature_head = parents[1]
        if _tree(root, feature_head) != _tree(root, current):
            raise ValidationRecoveryError(
                "Publication repair merge tree differs from its reviewed head"
            )
        publication_commit = current
    else:
        raise ValidationRecoveryError("Event cannot continue recovery publication")

    if _git(
        root,
        ["merge-base", "--is-ancestor", base, feature_head],
        operation="publication repair feature ancestry inspection",
        check=False,
    ).returncode != 0:
        raise ValidationRecoveryError("Publication repair feature head has invalid ancestry")
    changed = _changed_paths(root, base, feature_head)
    if changed != PUBLICATION_REPAIR_PATHS:
        raise ValidationRecoveryError(
            "Publication repair tree delta is outside the reviewed scope"
        )
    if _changed_paths(root, base, current) != PUBLICATION_REPAIR_PATHS:
        raise ValidationRecoveryError("Current publication repair tree delta changed")
    return {
        "base_commit": base,
        "feature_head": feature_head,
        "paths": _blob_records(root, current, PUBLICATION_REPAIR_PATHS),
        "publication_repair_commit": publication_commit,
        "publication_repair_tree": _tree(root, current),
        "pull_request": publication["pull_request"],
    }


def validate_sync_repair(
    root: Path,
    current_commit: str,
    *,
    event_name: str,
    pull_request: int | None,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate only the reviewed PR #57 branch or its merge onto PR #56."""

    root = root.resolve()
    current = _resolve(root, current_commit)
    repair = contract["sync_repair"]
    base = _resolve(root, repair["base_commit"])
    release = contract["release"]["commit"]
    validate_publication_repair(
        root,
        base,
        event_name="workflow_dispatch",
        pull_request=None,
        contract=contract,
    )
    _verify_release_identity(root, contract)
    if _git(
        root,
        ["merge-base", "--is-ancestor", base, current],
        operation="sync-head repair ancestry inspection",
        check=False,
    ).returncode != 0:
        raise ValidationRecoveryError("Synchronization-head repair is not based on PR #56")
    if _git(
        root,
        ["merge-base", "--is-ancestor", release, current],
        operation="sync-head repair release inspection",
        check=False,
    ).returncode == 0:
        raise ValidationRecoveryError(
            "Synchronization-head repair unexpectedly contains the release ledger"
        )

    if event_name == "pull_request":
        if pull_request != repair["pull_request"]:
            raise ValidationRecoveryError("Recovery is restricted to sync-head repair PR #57")
        feature_head = current
        repair_commit = None
    elif event_name in {"push", "workflow_dispatch"}:
        parents = _parents(root, current)
        if len(parents) != 2 or parents[0] != base:
            raise ValidationRecoveryError(
                "Synchronization-head repair is not a direct two-parent merge onto PR #56"
            )
        feature_head = parents[1]
        if _tree(root, feature_head) != _tree(root, current):
            raise ValidationRecoveryError(
                "Synchronization-head repair merge tree differs from its reviewed head"
            )
        repair_commit = current
    else:
        raise ValidationRecoveryError("Event cannot continue sync-head recovery")

    if _git(
        root,
        ["merge-base", "--is-ancestor", base, feature_head],
        operation="sync-head repair feature ancestry inspection",
        check=False,
    ).returncode != 0:
        raise ValidationRecoveryError("Synchronization-head repair feature has invalid ancestry")
    if _changed_paths(root, base, feature_head) != SYNC_REPAIR_PATHS:
        raise ValidationRecoveryError(
            "Synchronization-head repair tree delta is outside the reviewed scope"
        )
    if _changed_paths(root, base, current) != SYNC_REPAIR_PATHS:
        raise ValidationRecoveryError("Current synchronization-head repair delta changed")
    return {
        "base_commit": base,
        "feature_head": feature_head,
        "paths": _blob_records(root, current, SYNC_REPAIR_PATHS),
        "pull_request": repair["pull_request"],
        "sync_repair_commit": repair_commit,
        "sync_repair_tree": _tree(root, current),
    }


def validate_check_association_repair(
    root: Path,
    current_commit: str,
    *,
    event_name: str,
    pull_request: int | None,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate only PR #59 or its merge onto the completed PR #57 merge."""

    root = root.resolve()
    current = _resolve(root, current_commit)
    repair = contract["check_association_repair"]
    base = _resolve(root, repair["base_commit"])
    release = contract["release"]["commit"]
    sync_repair = validate_sync_repair(
        root,
        base,
        event_name="workflow_dispatch",
        pull_request=None,
        contract=contract,
    )
    _verify_release_identity(root, contract)
    if _git(
        root,
        ["merge-base", "--is-ancestor", base, current],
        operation="check-association repair ancestry inspection",
        check=False,
    ).returncode != 0:
        raise ValidationRecoveryError(
            "Check-association repair is not based on the PR #57 merge"
        )
    if _git(
        root,
        ["merge-base", "--is-ancestor", release, current],
        operation="check-association repair release inspection",
        check=False,
    ).returncode == 0:
        raise ValidationRecoveryError(
            "Check-association repair unexpectedly contains the release ledger"
        )

    if event_name == "pull_request":
        if pull_request != repair["pull_request"]:
            raise ValidationRecoveryError(
                "Recovery is restricted to check-association repair PR #59"
            )
        feature_head = current
        repair_commit = None
    elif event_name in {"push", "workflow_dispatch"}:
        parents = _parents(root, current)
        if len(parents) != 2 or parents[0] != base:
            raise ValidationRecoveryError(
                "Check-association repair is not a direct two-parent merge onto PR #57"
            )
        feature_head = parents[1]
        if _tree(root, feature_head) != _tree(root, current):
            raise ValidationRecoveryError(
                "Check-association repair merge tree differs from its reviewed head"
            )
        repair_commit = current
    else:
        raise ValidationRecoveryError("Event cannot continue check-association repair")

    if _git(
        root,
        ["merge-base", "--is-ancestor", base, feature_head],
        operation="check-association repair feature ancestry inspection",
        check=False,
    ).returncode != 0:
        raise ValidationRecoveryError(
            "Check-association repair feature has invalid ancestry"
        )
    if _changed_paths(root, base, feature_head) != CHECK_ASSOCIATION_REPAIR_PATHS:
        raise ValidationRecoveryError(
            "Check-association repair tree delta is outside the reviewed scope"
        )
    if _changed_paths(root, base, current) != CHECK_ASSOCIATION_REPAIR_PATHS:
        raise ValidationRecoveryError("Current check-association repair delta changed")
    return {
        "base_commit": base,
        "check_association_repair_commit": repair_commit,
        "check_association_repair_tree": _tree(root, current),
        "feature_head": feature_head,
        "paths": _blob_records(root, current, CHECK_ASSOCIATION_REPAIR_PATHS),
        "pull_request": repair["pull_request"],
        "sync_repair": sync_repair,
    }


def _merge_tree(root: Path, first_parent: str, second_parent: str) -> str:
    completed = _git(
        root,
        ["merge-tree", "--write-tree", first_parent, second_parent],
        operation="synthetic synchronization merge",
        check=False,
    )
    if completed.returncode != 0:
        raise ValidationRecoveryError("Published release does not merge cleanly")
    first_line = completed.stdout.splitlines()[0] if completed.stdout else ""
    if COMMIT_RE.fullmatch(first_line) is None:
        raise ValidationRecoveryError("Synthetic synchronization tree is invalid")
    return first_line


def _synthetic_commit(root: Path, tree: str, first: str, second: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_AUTHOR_EMAIL": "recovery-validation@example.invalid",
            "GIT_AUTHOR_NAME": "Recovery Validation",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_EMAIL": "recovery-validation@example.invalid",
            "GIT_COMMITTER_NAME": "Recovery Validation",
        }
    )
    value = _git(
        root,
        [
            "commit-tree",
            tree,
            "-p",
            first,
            "-p",
            second,
            "-m",
            "synthetic published-release synchronization",
        ],
        operation="synthetic synchronization commit",
        environment=environment,
    ).stdout.strip()
    return _commit(value, context="Synthetic synchronization commit")


def validate_sync_update(
    root: Path,
    sync_head_commit: str,
    repair_commit: str,
    *,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate PR #52's second bounded update without rewriting its first."""

    root = root.resolve()
    release = _resolve(root, contract["release"]["commit"])
    repair = _resolve(root, repair_commit)
    sync_head = _resolve(root, sync_head_commit)
    repair_evidence = validate_check_association_repair(
        root,
        repair,
        event_name="workflow_dispatch",
        pull_request=None,
        contract=contract,
    )
    sync = contract["sync"]
    prior_head = _resolve(root, sync["prior_head_commit"])
    prior_base = repair_evidence["base_commit"]
    prior_tree = _tree(root, prior_head)
    if (
        _parents(root, prior_head) != [release, prior_base]
        or prior_tree != sync["prior_head_tree"]
        or prior_tree != _merge_tree(root, release, prior_base)
    ):
        raise ValidationRecoveryError(
            "Previously completed synchronization update identity changed"
        )
    expected_tree = _merge_tree(root, prior_head, repair)
    if _parents(root, sync_head) != [prior_head, repair]:
        raise ValidationRecoveryError(
            "Synchronization update is not the permitted ordered continuation of reviewed main"
        )
    if _tree(root, sync_head) != expected_tree:
        raise ValidationRecoveryError("Synchronization update merge tree changed")
    return {
        "base_commit": repair,
        "check_association_repair": repair_evidence,
        "initial_head_commit": sync["initial_head_commit"],
        "previous_head_commit": prior_head,
        "pull_request": sync["pull_request"],
        "release_commit": release,
        "sync_branch": sync["branch"],
        "sync_head_commit": sync_head,
        "sync_head_tree": expected_tree,
        "sync_repair": repair_evidence["sync_repair"],
    }


def validate_sync_merge(
    root: Path,
    merge_commit: str,
    *,
    sync_head_commit: str,
    repair_commit: str,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the final PR #52 merge without changing the release selection."""

    root = root.resolve()
    merged = _resolve(root, merge_commit)
    update = validate_sync_update(
        root,
        sync_head_commit,
        repair_commit,
        contract=contract,
    )
    if _parents(root, merged) != [update["base_commit"], update["sync_head_commit"]]:
        raise ValidationRecoveryError(
            "Synchronization PR merge has unexpected ordered parents"
        )
    expected_tree = _merge_tree(
        root, update["base_commit"], update["sync_head_commit"]
    )
    if _tree(root, merged) != expected_tree:
        raise ValidationRecoveryError("Synchronization PR merge tree changed")
    return {
        "merge_commit": merged,
        "merge_tree": expected_tree,
        "sync_update": update,
    }


def verify_pending_ledger(
    root: Path,
    current_commit: str,
    *,
    event_name: str,
    pull_request: int | None,
    contract: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate only the exact remote-ledger gap and its future merge shape."""

    expected_error = (
        "Stale main release ledger: release/platform-v0.6.2 is published remotely "
        "but absent from the source commit; merge the published release "
        "synchronization PR into main first"
    )
    try:
        ledger.verify_ledger(
            root,
            current_commit,
            remote="origin",
            release_root="releases/platform",
            environ=environ,
        )
    except ledger.LedgerError as exc:
        if str(exc) != expected_error:
            raise ValidationRecoveryError(
                "Ordinary release-ledger validation failed for another reason"
            ) from exc
    else:
        raise ValidationRecoveryError("Published-release recovery is no longer pending")

    root = root.resolve()
    current = _resolve(root, current_commit)
    repair_contract = contract["check_association_repair"]
    if event_name == "pull_request":
        if pull_request != repair_contract["pull_request"]:
            raise ValidationRecoveryError(
                "Pending ledger recovery is restricted to check-association repair PR #59"
            )
        repair_feature = validate_check_association_repair(
            root,
            current,
            event_name=event_name,
            pull_request=pull_request,
            contract=contract,
        )
        repair_tree = _tree(root, current)
        repair_commit = _synthetic_commit(
            root,
            repair_tree,
            repair_contract["base_commit"],
            current,
        )
        association_repair = {
            **repair_feature,
            "check_association_repair_commit": repair_commit,
        }
    else:
        association_repair = validate_check_association_repair(
            root,
            current,
            event_name=event_name,
            pull_request=pull_request,
            contract=contract,
        )
        repair_commit = current

    prior_head = contract["sync"]["prior_head_commit"]
    update_tree = _merge_tree(root, prior_head, repair_commit)
    synthetic_update = _synthetic_commit(
        root, update_tree, prior_head, repair_commit
    )
    sync_update = validate_sync_update(
        root,
        synthetic_update,
        repair_commit,
        contract=contract,
    )
    final_tree = _merge_tree(root, repair_commit, synthetic_update)
    synthetic = _synthetic_commit(
        root, final_tree, repair_commit, synthetic_update
    )
    with tempfile.TemporaryDirectory(prefix="buh-ledger-recovery-") as temporary:
        worktree = Path(temporary) / "checkout"
        _git(
            root,
            ["worktree", "add", "--detach", str(worktree), synthetic],
            operation="synthetic worktree creation",
        )
        try:
            state = ledger.verify_ledger(
                worktree,
                synthetic,
                remote="origin",
                release_root="releases/platform",
                environ=environ,
            )
            previous = worktree / "releases/platform/v0.6.2/RELEASE.json"
            plan = buh_release.create_plan_v1(
                repo_root=worktree,
                registry_path=worktree / "ops/release/apps.toml",
                compatibility_path=None,
                changes_dir=worktree / "changes",
                previous_manifest_path=previous,
                source_commit=synthetic,
            )
        finally:
            _git(
                root,
                ["worktree", "remove", "--force", str(worktree)],
                operation="synthetic worktree cleanup",
                check=False,
            )
    if (
        state.get("latest", {}).get("platform_version") != "0.6.2"
        or plan.get("previous_platform_version") != "0.6.2"
        or plan.get("platform_version") != "0.6.3"
        or plan.get("release_required") is not True
    ):
        raise ValidationRecoveryError("Post-synchronization release plan is invalid")
    return {
        "activation": verify_activation(root, contract=contract),
        "continuation": verify_continuation(root, contract=contract),
        "latest_release": {
            key: state["latest"][key]
            for key in ("manifest_sha256", "platform_version", "release_commit")
        },
        "next_release": {
            "deployment_predecessor": plan.get("deployment_predecessor"),
            "platform_version": plan["platform_version"],
            "previous_platform_version": plan["previous_platform_version"],
            "release_required": plan["release_required"],
        },
        "recovery_id": contract["recovery_id"],
        "publication_repair": validate_publication_repair(
            root,
            contract["sync_repair"]["base_commit"],
            event_name="workflow_dispatch",
            pull_request=None,
            contract=contract,
        ),
        "repair": validate_repair(
            root,
            contract["publication_repair"]["base_commit"],
            event_name="workflow_dispatch",
            pull_request=None,
            contract=contract,
        ),
        "schema_version": SCHEMA_VERSION,
        "source_commit": current,
        "check_association_repair": association_repair,
        "sync_repair": association_repair["sync_repair"],
        "sync_update": sync_update,
        "synthetic_sync_update_commit": synthetic_update,
        "synthetic_sync_update_tree": update_tree,
        "synthetic_merge_commit": synthetic,
        "synthetic_merge_tree": final_tree,
    }


def validate_hold(
    root: Path,
    current_commit: str,
    *,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Hold v0.6.3 across the check-association repair and final sync."""

    current = _resolve(root, current_commit)
    # Continue the hold from the completed PR #61 merge; this log-reader repair
    # does not authorize recovery, cleanup, a release build or a deployment.
    log_repair_base = "4f02900aed9e62c3f7fcb7cce4bcdde50325b5bc"
    parents = _parents(root, current)
    if parents[:1] == [log_repair_base]:
        permitted = {
            "ops/deploy/docker_host.py", "ops/deploy/worker_recovery.py",
            "ops/deploy/WORKER-RECOVERY.md", "ops/release/validation_recovery.py",
            "tests/deploy/test_docker_host.py", "tests/deploy/test_log_streaming.py",
            "tests/deploy/test_worker_completion.py", "tests/deploy/test_log_reader_install.py",
            "tests/platform/test_coordinated_recovery_rehearsal.py",
            "tests/release/test_worker_repair_hold.py",
            "changes/retained-recovery-log-streaming.toml",
        }
        changed = set(_changed_paths(root, log_repair_base, current))
        if (
            len(parents) != 2 or not changed or not changed <= permitted
            or _tree(root, parents[1]) != _tree(root, current)
        ):
            raise ValidationRecoveryError("Retained log repair is outside the bounded release hold")
        previous = validate_hold(root, log_repair_base, contract=contract)
        return {**previous, "current_commit": current,
                "retained_log_repair": {"base_commit": log_repair_base,
                                        "feature_head": parents[1], "merge_commit": current}}
    # PR #60's one-use continuation reached migrations and failed rollback
    # verification. A reviewed receiver correction must not build v0.6.3 or
    # consume fragments while that recovery remains unresolved. This is a hold
    # only: it does not reopen the consumed production authorization.
    worker_repair_base = "efdeebf9ff86a97cef18463605273a032403c4f0"
    if parents[:1] == [worker_repair_base]:
        permitted = {
            "ops/deploy/docker_host.py", "ops/deploy/collect_worker_recovery.py",
            "ops/deploy/worker_recovery.py", "tests/deploy/test_worker_completion.py",
            "ops/deploy/README.md", "ops/deploy/WORKER-RECOVERY.md",
            "ops/release/README.md", "ops/release/validation_recovery.py",
            "changes/celery-worker-identity-recovery.toml",
            "tests/deploy/test_docker_host.py", "tests/deploy/test_celery_identity.py",
            "tests/deploy/test_worker_recovery.py", "tests/deploy/rehearse_celery_workers.py",
            "tests/deploy/fixtures/celery-v062-workers.json",
            "tests/integration/test_celery_nodenames.py",
            "tests/release/test_worker_repair_hold.py",
            "platform/testenv/compose.yml", "platform/testenv/Dockerfile",
            "platform/testenv/run-integration.sh", ".github/workflows/reusable-source-tests.yml",
        }
        changed = set(_changed_paths(root, worker_repair_base, current))
        if (
            len(parents) != 2 or not changed or not changed <= permitted
            or _tree(root, parents[1]) != _tree(root, current)
        ):
            raise ValidationRecoveryError("Worker identity repair is outside the bounded release hold")
        previous = validate_hold(root, worker_repair_base, contract=contract)
        return {**previous, "current_commit": current, "state": "worker-recovery-unverified-held",
                "worker_repair": {"base_commit": worker_repair_base,
                                  "feature_head": parents[1], "merge_commit": current},
                "production_authorization": "consumed-requires-new-review-and-approval"}
    # The approved sync is now merged. Hold the next release across its one
    # reviewed, non-runtime deployment repair as well; never consume fragments.
    if _parents(root, current)[:1] == ["73569d32dc4f64fc1733cfc00b1d4c48928d5c5c"]:
        import approved_deployment_continuation

        repair = approved_deployment_continuation.validate_repair(root, current)
        previous = validate_hold(root, repair["base_commit"], contract=contract)
        return {
            **previous,
            "current_commit": current,
            "deployment_repair": repair,
            "state": "approved-deployment-continuation-held",
        }
    try:
        association_repair = validate_check_association_repair(
            root,
            current,
            event_name="workflow_dispatch",
            pull_request=None,
            contract=contract,
        )
        state = "check-association-repair-pending-update"
        repair_commit = current
        sync_update = None
    except ValidationRecoveryError:
        parents = _parents(root, current)
        if len(parents) != 2:
            raise ValidationRecoveryError("Current main is outside the recovery hold")
        repair_commit, sync_head = parents
        merged = validate_sync_merge(
            root,
            current,
            sync_head_commit=sync_head,
            repair_commit=repair_commit,
            contract=contract,
        )
        sync_update = merged["sync_update"]
        association_repair = sync_update["check_association_repair"]
        state = "synchronized-awaiting-retirement"
    return {
        "activation": verify_activation(root, contract=contract),
        "continuation": verify_continuation(root, contract=contract),
        "current_commit": current,
        "publication_repair": validate_publication_repair(
            root,
            contract["sync_repair"]["base_commit"],
            event_name="workflow_dispatch",
            pull_request=None,
            contract=contract,
        ),
        "publication_repair_commit": contract["sync_repair"]["base_commit"],
        "recovery_id": contract["recovery_id"],
        "repair": validate_repair(
            root,
            contract["publication_repair"]["base_commit"],
            event_name="workflow_dispatch",
            pull_request=None,
            contract=contract,
        ),
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "check_association_repair": association_repair,
        "check_association_repair_commit": repair_commit,
        "sync_repair": association_repair["sync_repair"],
        "sync_update": sync_update,
    }


def apply_harness(
    root: Path,
    harness_commit: str,
    *,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Overlay reviewed fixtures and isolated support onto exact v0.6.2."""

    root = root.resolve()
    release = contract["release"]["commit"]
    if _resolve(root, "HEAD") != release:
        raise ValidationRecoveryError("Harness target is not exact published v0.6.2")
    if _git(
        root,
        ["status", "--porcelain=v1", "--untracked-files=all"],
        operation="harness worktree inspection",
    ).stdout:
        raise ValidationRecoveryError("Harness target worktree is not clean")
    repair = validate_repair(
        root,
        harness_commit,
        event_name="workflow_dispatch",
        pull_request=None,
        contract=contract,
    )
    files = []
    for path in HARNESS_PATHS:
        object_id, content = _blob(root, harness_commit, path)
        destination = root.joinpath(*PurePosixPath(path).parts)
        if not destination.is_file() or destination.is_symlink():
            raise ValidationRecoveryError("Harness target path is unsafe")
        temporary = destination.with_name(f".{destination.name}.recovery")
        try:
            temporary.write_bytes(content)
            temporary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            os.replace(temporary, destination)
        except OSError as exc:
            raise ValidationRecoveryError("Could not apply recovery harness") from exc
        finally:
            if temporary.exists():
                temporary.unlink()
        files.append(
            {
                "git_blob_sha": object_id,
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    support_root = root / TEST_SUPPORT_ROOT
    if support_root.exists() or support_root.is_symlink():
        raise ValidationRecoveryError("Recovery test-support root already exists")
    support_files = []
    try:
        support_root.mkdir(mode=0o755)
        for path in TEST_SUPPORT_PATHS:
            object_id, content = _blob(root, harness_commit, path)
            staged_path = f"{TEST_SUPPORT_ROOT}/{path}"
            destination = root.joinpath(*PurePosixPath(staged_path).parts)
            destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                raise ValidationRecoveryError("Recovery test-support path is unsafe")
            destination.write_bytes(content)
            destination.chmod(
                stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
            )
            support_files.append(
                {
                    "git_blob_sha": object_id,
                    "path": path,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "staged_path": staged_path,
                }
            )
    except (OSError, ValidationRecoveryError) as exc:
        if isinstance(exc, ValidationRecoveryError):
            raise
        raise ValidationRecoveryError("Could not stage recovery test support") from exc
    changed = _changed_paths(root, release, "HEAD")
    # `git diff <commit> HEAD` cannot see worktree edits; inspect the index/worktree.
    status = _git_bytes(
        root,
        ["diff", "--name-only", "-z", "--"],
        operation="applied harness inspection",
    )
    actual = tuple(
        sorted(item.decode("utf-8") for item in status.split(b"\0") if item)
    )
    untracked = _git_bytes(
        root,
        ["ls-files", "--others", "--exclude-standard", "-z", "--"],
        operation="staged recovery support inspection",
    )
    actual_support = tuple(
        sorted(item.decode("utf-8") for item in untracked.split(b"\0") if item)
    )
    expected_support = tuple(
        f"{TEST_SUPPORT_ROOT}/{path}" for path in TEST_SUPPORT_PATHS
    )
    if changed or actual != HARNESS_PATHS or actual_support != expected_support:
        raise ValidationRecoveryError("Harness changed files outside its reviewed scope")
    return {
        "activation": verify_activation(root, contract=contract),
        "continuation": verify_continuation(root, contract=contract),
        "files": files,
        "harness_commit": harness_commit,
        "recovery_id": contract["recovery_id"],
        "release_commit": release,
        "release_tree": contract["release"]["tree"],
        "repair": repair,
        "schema_version": SCHEMA_VERSION,
        "test_support": {
            "files": support_files,
            "root": TEST_SUPPORT_ROOT,
        },
    }


def create_attestation(
    root: Path,
    repair_commit: str,
    *,
    repository: str,
    run_id: int,
    run_attempt: int,
    actor: str,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    if repository != contract["repository"]:
        raise ValidationRecoveryError("Recovery workflow repository changed")
    _positive_int(run_id, context="Recovery workflow run ID")
    if _positive_int(run_attempt, context="Recovery workflow run attempt") != 1:
        raise ValidationRecoveryError("Recovery workflow reruns are forbidden")
    if LOGIN_RE.fullmatch(actor) is None:
        raise ValidationRecoveryError("Recovery workflow actor is invalid")
    repair = validate_repair(
        root,
        repair_commit,
        event_name="workflow_dispatch",
        pull_request=None,
        contract=contract,
    )
    files = _blob_records(root, repair_commit, HARNESS_PATHS)
    support_files = []
    for item in _blob_records(root, repair_commit, TEST_SUPPORT_PATHS):
        support_files.append(
            {
                **item,
                "staged_path": f"{TEST_SUPPORT_ROOT}/{item['path']}",
            }
        )
    return {
        "activation": verify_activation(root, contract=contract),
        "continuation": verify_continuation(root, contract=contract),
        "harness": {
            "commit": repair_commit,
            "files": files,
            "test_support": {
                "files": support_files,
                "root": TEST_SUPPORT_ROOT,
            },
            "tree": repair["repair_tree"],
        },
        "recovery_id": contract["recovery_id"],
        "release": dict(contract["release"]),
        "repository": repository,
        "repair": repair,
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "validation": {
            "actor": actor,
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": repair_commit,
            "result": "success",
            "run_attempt": run_attempt,
            "run_id": run_id,
            "workflow_path": WORKFLOW_PATH,
        },
    }


def validate_attestation_data(
    value: Any,
    *,
    contract: Mapping[str, Any],
    expected_run_id: int | None = None,
    expected_run_attempt: int | None = None,
    expected_repair: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "activation",
        "continuation",
        "harness",
        "recovery_id",
        "release",
        "repository",
        "repair",
        "schema_version",
        "validation",
    }:
        raise ValidationRecoveryError("Validation recovery attestation has invalid fields")
    if (
        value.get("schema_version") != ATTESTATION_SCHEMA_VERSION
        or value.get("recovery_id") != contract["recovery_id"]
        or value.get("repository") != contract["repository"]
        or value.get("release") != contract["release"]
    ):
        raise ValidationRecoveryError("Validation recovery attestation identity changed")
    activation = value.get("activation")
    if not isinstance(activation, dict) or set(activation) != {
        "activation_commit",
        "activation_tree",
        "base_commit",
        "feature_head",
        "paths",
        "pull_request",
    }:
        raise ValidationRecoveryError("Validation recovery activation is invalid")
    activation_commit = _commit(
        activation.get("activation_commit"), context="Attested activation commit"
    )
    if (
        activation_commit != contract["activation"]["commit"]
        or activation.get("activation_tree") != contract["activation"]["tree"]
        or activation.get("base_commit") != contract["activation"]["base_commit"]
        or activation.get("feature_head") != contract["activation"]["feature_head"]
        or activation.get("pull_request") != contract["activation"]["pull_request"]
    ):
        raise ValidationRecoveryError("Validation recovery activation identity changed")
    paths = activation.get("paths")
    if not isinstance(paths, list) or [item.get("path") for item in paths] != list(
        ACTIVATION_PATHS
    ):
        raise ValidationRecoveryError("Attested activation path scope changed")
    for item in paths:
        if not isinstance(item, dict) or set(item) != {
            "git_blob_sha",
            "path",
            "sha256",
        }:
            raise ValidationRecoveryError("Attested activation blob is invalid")
        _commit(item.get("git_blob_sha"), context="Activation blob")
        _sha256(item.get("sha256"), context="Activation blob digest")

    continuation = value.get("continuation")
    if not isinstance(continuation, dict) or set(continuation) != {
        "base_commit",
        "continuation_commit",
        "continuation_tree",
        "feature_head",
        "paths",
        "pull_request",
    }:
        raise ValidationRecoveryError("Validation recovery continuation is invalid")
    continuation_commit = _commit(
        continuation.get("continuation_commit"),
        context="Attested continuation commit",
    )
    if (
        continuation.get("base_commit") != activation_commit
        or continuation_commit != contract["continuation"]["commit"]
        or continuation.get("continuation_tree")
        != contract["continuation"]["tree"]
        or continuation.get("feature_head")
        != contract["continuation"]["feature_head"]
        or continuation.get("pull_request") != contract["continuation"]["pull_request"]
    ):
        raise ValidationRecoveryError("Validation recovery continuation identity changed")
    continuation_paths = continuation.get("paths")
    if not isinstance(continuation_paths, list) or [
        item.get("path") for item in continuation_paths
    ] != list(CONTINUATION_PATHS):
        raise ValidationRecoveryError("Attested continuation path scope changed")
    for item in continuation_paths:
        if not isinstance(item, dict) or set(item) != {
            "git_blob_sha",
            "path",
            "sha256",
        }:
            raise ValidationRecoveryError("Attested continuation blob is invalid")
        _commit(item.get("git_blob_sha"), context="Continuation blob")
        _sha256(item.get("sha256"), context="Continuation blob digest")

    repair = value.get("repair")
    if not isinstance(repair, dict) or set(repair) != {
        "base_commit",
        "feature_head",
        "paths",
        "pull_request",
        "repair_commit",
        "repair_tree",
    }:
        raise ValidationRecoveryError("Validation recovery digest repair is invalid")
    repair_commit = _commit(
        repair.get("repair_commit"), context="Attested digest repair commit"
    )
    if (
        repair.get("base_commit") != continuation_commit
        or repair.get("pull_request") != contract["repair"]["pull_request"]
        or COMMIT_RE.fullmatch(str(repair.get("repair_tree"))) is None
        or COMMIT_RE.fullmatch(str(repair.get("feature_head"))) is None
        or expected_repair is not None
        and repair_commit != expected_repair
    ):
        raise ValidationRecoveryError("Validation recovery digest repair identity changed")
    repair_paths = repair.get("paths")
    if not isinstance(repair_paths, list) or [
        item.get("path") for item in repair_paths
    ] != list(REPAIR_PATHS):
        raise ValidationRecoveryError("Attested digest repair path scope changed")
    for item in repair_paths:
        if not isinstance(item, dict) or set(item) != {
            "git_blob_sha",
            "path",
            "sha256",
        }:
            raise ValidationRecoveryError("Attested digest repair blob is invalid")
        _commit(item.get("git_blob_sha"), context="Digest repair blob")
        _sha256(item.get("sha256"), context="Digest repair blob digest")

    harness = value.get("harness")
    if not isinstance(harness, dict) or set(harness) != {
        "commit",
        "files",
        "test_support",
        "tree",
    }:
        raise ValidationRecoveryError("Validation recovery harness is invalid")
    if (
        harness.get("commit") != repair_commit
        or harness.get("tree") != repair.get("repair_tree")
    ):
        raise ValidationRecoveryError("Validation recovery harness identity changed")
    files = harness.get("files")
    if not isinstance(files, list) or [item.get("path") for item in files] != list(
        HARNESS_PATHS
    ):
        raise ValidationRecoveryError("Validation recovery harness scope changed")
    for item in files:
        if not isinstance(item, dict) or set(item) != {
            "git_blob_sha",
            "path",
            "sha256",
        }:
            raise ValidationRecoveryError("Validation recovery harness blob is invalid")
        _commit(item.get("git_blob_sha"), context="Harness blob")
        _sha256(item.get("sha256"), context="Harness blob digest")
    reviewed_by_path = {item["path"]: item for item in paths}
    reviewed_by_path.update({item["path"]: item for item in continuation_paths})
    reviewed_by_path.update({item["path"]: item for item in repair_paths})
    if any(reviewed_by_path.get(item["path"]) != item for item in files):
        raise ValidationRecoveryError("Harness bytes differ from the digest repair commit")
    test_support = harness.get("test_support")
    if not isinstance(test_support, dict) or set(test_support) != {"files", "root"}:
        raise ValidationRecoveryError("Validation recovery test support is invalid")
    if test_support.get("root") != TEST_SUPPORT_ROOT:
        raise ValidationRecoveryError("Validation recovery test-support root changed")
    support_files = test_support.get("files")
    if not isinstance(support_files, list) or [
        item.get("path") for item in support_files
    ] != list(TEST_SUPPORT_PATHS):
        raise ValidationRecoveryError("Validation recovery test-support scope changed")
    for path, item in zip(TEST_SUPPORT_PATHS, support_files, strict=True):
        if not isinstance(item, dict) or set(item) != {
            "git_blob_sha",
            "path",
            "sha256",
            "staged_path",
        }:
            raise ValidationRecoveryError("Validation recovery support blob is invalid")
        if item.get("staged_path") != f"{TEST_SUPPORT_ROOT}/{path}":
            raise ValidationRecoveryError("Validation recovery support path changed")
        _commit(item.get("git_blob_sha"), context="Test-support blob")
        _sha256(item.get("sha256"), context="Test-support blob digest")

    validation = value.get("validation")
    if not isinstance(validation, dict) or set(validation) != {
        "actor",
        "event",
        "head_branch",
        "head_sha",
        "result",
        "run_attempt",
        "run_id",
        "workflow_path",
    }:
        raise ValidationRecoveryError("Validation recovery workflow identity is invalid")
    run_id = _positive_int(validation.get("run_id"), context="Recovery run ID")
    attempt = _positive_int(
        validation.get("run_attempt"), context="Recovery run attempt"
    )
    if (
        attempt != 1
        or validation.get("event") != "workflow_dispatch"
        or validation.get("head_branch") != "main"
        or validation.get("head_sha") != repair_commit
        or validation.get("result") != "success"
        or validation.get("workflow_path") != WORKFLOW_PATH
        or not isinstance(validation.get("actor"), str)
        or LOGIN_RE.fullmatch(validation["actor"]) is None
        or expected_run_id is not None
        and run_id != expected_run_id
        or expected_run_attempt is not None
        and attempt != expected_run_attempt
    ):
        raise ValidationRecoveryError("Validation recovery workflow identity changed")
    return value


def load_attestation(
    path: Path,
    *,
    contract: Mapping[str, Any],
    expected_run_id: int | None = None,
    expected_run_attempt: int | None = None,
    expected_repair: str | None = None,
) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValidationRecoveryError("Validation recovery attestation is unavailable") from exc
    if not raw or len(raw) > 512 * 1024:
        raise ValidationRecoveryError("Validation recovery attestation has an unsafe size")
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationRecoveryError("Validation recovery attestation is unreadable") from exc
    if raw != canonical_json_bytes(value):
        raise ValidationRecoveryError("Validation recovery attestation is not canonical")
    return validate_attestation_data(
        value,
        contract=contract,
        expected_run_id=expected_run_id,
        expected_run_attempt=expected_run_attempt,
        expected_repair=expected_repair,
    )


def _write(path: Path, value: Mapping[str, Any]) -> None:
    encoded = canonical_json_bytes(value)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
    except OSError as exc:
        raise ValidationRecoveryError("Could not write validation recovery evidence") from exc


def _append_outputs(values: Mapping[str, Any]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    safe = re.compile(r"^[A-Za-z0-9._:/+-]{1,512}$")
    if any(safe.fullmatch(str(value)) is None for value in values.values()):
        raise ValidationRecoveryError("Refusing unsafe recovery workflow output")
    try:
        with Path(output).open("a", encoding="utf-8", newline="\n") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")
    except OSError as exc:
        raise ValidationRecoveryError("Could not append recovery workflow outputs") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in (
        "repair",
        "publication-repair",
        "sync-repair",
        "check-association-repair",
        "ledger",
        "hold",
    ):
        command = subparsers.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--source-commit", required=True)
        command.add_argument("--output", type=Path, required=True)
        if name in {
            "repair",
            "publication-repair",
            "sync-repair",
            "check-association-repair",
            "ledger",
        }:
            command.add_argument(
                "--event-name",
                choices=("pull_request", "push", "workflow_dispatch"),
                required=True,
            )
            command.add_argument("--pull-request", type=int)
    sync_update = subparsers.add_parser("sync-update")
    sync_update.add_argument("--root", type=Path, required=True)
    sync_update.add_argument("--source-commit", required=True)
    sync_update.add_argument("--repair-commit", required=True)
    sync_update.add_argument("--output", type=Path, required=True)
    overlay = subparsers.add_parser("apply-harness")
    overlay.add_argument("--root", type=Path, required=True)
    overlay.add_argument("--harness-commit", required=True)
    overlay.add_argument("--output", type=Path, required=True)
    attest = subparsers.add_parser("attest")
    attest.add_argument("--root", type=Path, required=True)
    attest.add_argument("--repair-commit", required=True)
    attest.add_argument("--repository", required=True)
    attest.add_argument("--run-id", type=int, required=True)
    attest.add_argument("--run-attempt", type=int, required=True)
    attest.add_argument("--actor", required=True)
    attest.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify-attestation")
    verify.add_argument("--attestation", type=Path, required=True)
    verify.add_argument("--run-id", type=int, required=True)
    verify.add_argument("--run-attempt", type=int, required=True)
    verify.add_argument("--repair-commit", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        contract = load_contract(arguments.contract)
        if arguments.command == "repair":
            repair = validate_repair(
                arguments.root,
                arguments.source_commit,
                event_name=arguments.event_name,
                pull_request=arguments.pull_request,
                contract=contract,
            )
            report = {
                "activation": verify_activation(arguments.root, contract=contract),
                "continuation": verify_continuation(
                    arguments.root, contract=contract
                ),
                "recovery_id": contract["recovery_id"],
                "repair": repair,
                "schema_version": SCHEMA_VERSION,
            }
            _write(arguments.output, report)
            _append_outputs(
                {
                    "activation_commit": report["activation"]["activation_commit"],
                    "activation_feature_head": report["activation"]["feature_head"],
                    "continuation_commit": report["continuation"][
                        "continuation_commit"
                    ],
                    "continuation_feature_head": report["continuation"][
                        "feature_head"
                    ],
                    "release_commit": contract["release"]["commit"],
                    "repair_commit": repair["repair_commit"] or "feature",
                    "repair_feature_head": repair["feature_head"],
                    "source_commit": contract["release"]["source_commit"],
                }
            )
        elif arguments.command == "publication-repair":
            publication = validate_publication_repair(
                arguments.root,
                arguments.source_commit,
                event_name=arguments.event_name,
                pull_request=arguments.pull_request,
                contract=contract,
            )
            report = {
                "publication_repair": publication,
                "recovery_id": contract["recovery_id"],
                "repair": validate_repair(
                    arguments.root,
                    contract["publication_repair"]["base_commit"],
                    event_name="workflow_dispatch",
                    pull_request=None,
                    contract=contract,
                ),
                "schema_version": SCHEMA_VERSION,
            }
            _write(arguments.output, report)
            _append_outputs(
                {
                    "publication_repair_commit": publication[
                        "publication_repair_commit"
                    ]
                    or "feature",
                    "publication_repair_feature_head": publication["feature_head"],
                    "release_commit": contract["release"]["commit"],
                    "repair_commit": contract["publication_repair"]["base_commit"],
                    "source_commit": contract["release"]["source_commit"],
                }
            )
        elif arguments.command == "sync-repair":
            sync_repair = validate_sync_repair(
                arguments.root,
                arguments.source_commit,
                event_name=arguments.event_name,
                pull_request=arguments.pull_request,
                contract=contract,
            )
            report = {
                "recovery_id": contract["recovery_id"],
                "schema_version": SCHEMA_VERSION,
                "sync_repair": sync_repair,
            }
            _write(arguments.output, report)
            _append_outputs(
                {
                    "release_commit": contract["release"]["commit"],
                    "source_commit": contract["release"]["source_commit"],
                    "sync_repair_commit": sync_repair["sync_repair_commit"]
                    or "feature",
                    "sync_repair_feature_head": sync_repair["feature_head"],
                }
            )
        elif arguments.command == "check-association-repair":
            association_repair = validate_check_association_repair(
                arguments.root,
                arguments.source_commit,
                event_name=arguments.event_name,
                pull_request=arguments.pull_request,
                contract=contract,
            )
            report = {
                "check_association_repair": association_repair,
                "recovery_id": contract["recovery_id"],
                "schema_version": SCHEMA_VERSION,
            }
            _write(arguments.output, report)
            _append_outputs(
                {
                    "check_association_repair_commit": (
                        association_repair["check_association_repair_commit"]
                        or "feature"
                    ),
                    "check_association_repair_feature_head": association_repair[
                        "feature_head"
                    ],
                    "release_commit": contract["release"]["commit"],
                    "source_commit": contract["release"]["source_commit"],
                    "sync_repair_commit": association_repair["sync_repair"][
                        "sync_repair_commit"
                    ],
                    "sync_repair_feature_head": association_repair["sync_repair"][
                        "feature_head"
                    ],
                }
            )
        elif arguments.command == "sync-update":
            sync_update = validate_sync_update(
                arguments.root,
                arguments.source_commit,
                arguments.repair_commit,
                contract=contract,
            )
            report = {
                "recovery_id": contract["recovery_id"],
                "schema_version": SCHEMA_VERSION,
                "sync_update": sync_update,
            }
            _write(arguments.output, report)
            _append_outputs(
                {
                    "release_commit": sync_update["release_commit"],
                    "source_commit": contract["release"]["source_commit"],
                    "sync_head_commit": sync_update["sync_head_commit"],
                    "check_association_repair_commit": sync_update["base_commit"],
                    "previous_sync_head_commit": sync_update[
                        "previous_head_commit"
                    ],
                }
            )
        elif arguments.command == "ledger":
            report = verify_pending_ledger(
                arguments.root,
                arguments.source_commit,
                event_name=arguments.event_name,
                pull_request=arguments.pull_request,
                contract=contract,
                environ=os.environ,
            )
            _write(arguments.output, report)
            _append_outputs(
                {
                    "bootstrap": "false",
                    "recovery": "true",
                    "recovery_plan": arguments.output,
                }
            )
        elif arguments.command == "hold":
            report = validate_hold(
                arguments.root,
                arguments.source_commit,
                contract=contract,
            )
            _write(arguments.output, report)
            _append_outputs({"hold": "true", "state": report["state"]})
        elif arguments.command == "apply-harness":
            report = apply_harness(
                arguments.root,
                arguments.harness_commit,
                contract=contract,
            )
            _write(arguments.output, report)
        elif arguments.command == "attest":
            report = create_attestation(
                arguments.root,
                arguments.repair_commit,
                repository=arguments.repository,
                run_id=arguments.run_id,
                run_attempt=arguments.run_attempt,
                actor=arguments.actor,
                contract=contract,
            )
            _write(arguments.output, report)
            _append_outputs(
                {
                    "activation_commit": contract["activation"]["commit"],
                    "continuation_commit": contract["continuation"]["commit"],
                    "release_commit": contract["release"]["commit"],
                    "repair_commit": arguments.repair_commit,
                    "source_commit": contract["release"]["source_commit"],
                }
            )
        else:
            load_attestation(
                arguments.attestation,
                contract=contract,
                expected_run_id=arguments.run_id,
                expected_run_attempt=arguments.run_attempt,
                expected_repair=arguments.repair_commit,
            )
            report = {
                "recovery_id": contract["recovery_id"],
                "result": "verified",
                "schema_version": SCHEMA_VERSION,
            }
        sys.stdout.write(canonical_json_bytes(report).decode("ascii"))
        return 0
    except (
        ValidationRecoveryError,
        ledger.LedgerError,
        buh_release.ReleaseError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        sys.stderr.write(
            f"validation recovery error: {str(exc).splitlines()[0][:500]}\n"
        )
        return 2
    except Exception:
        sys.stderr.write("validation recovery error: unexpected internal failure\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
