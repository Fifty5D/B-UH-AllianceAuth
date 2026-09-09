#!/usr/bin/env python3
"""Bounded validation recovery for the already-published platform v0.6.2.

This module does not publish, rebuild, approve, merge, or deploy a release.  It
validates the one reviewed recovery contract, materializes a disposable future
ledger shape, applies the two-file test-only harness overlay, and emits canonical
evidence for the trusted approval verifier.
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


SCHEMA_VERSION = 1
ATTESTATION_SCHEMA_VERSION = 1
RECOVERY_ID = "published-platform-v0.6.2-validation-20260909"
WORKFLOW_PATH = ".github/workflows/source-published-release-recovery.yml"
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
        "feature",
        "main_validation",
        "platform_version",
        "preflight",
        "required_check",
        "recovery_id",
        "release",
        "repository",
        "schema_version",
        "sync",
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
        "harness_paths",
        "pull_request",
    }:
        raise ValidationRecoveryError("Recovery activation contract is invalid")
    if (
        _positive_int(activation.get("pull_request"), context="Activation PR") != 53
        or _commit(activation.get("base_commit"), context="Activation base")
        != "4f98e7cb559ee1a9b269ea1f94938678d2dac4df"
        or activation.get("allowed_paths") != list(ACTIVATION_PATHS)
        or activation.get("harness_paths") != list(HARNESS_PATHS)
    ):
        raise ValidationRecoveryError("Recovery activation scope changed")

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
    if not isinstance(sync, dict) or set(sync) != {"branch", "pull_request"}:
        raise ValidationRecoveryError("Recovery synchronization identity is invalid")
    if sync != {"branch": "sync/platform-v0.6.2", "pull_request": 52}:
        raise ValidationRecoveryError("Recovery synchronization identity changed")

    preflight = value.get("preflight")
    if not isinstance(preflight, dict) or set(preflight) != {
        "artifact_digest",
        "artifact_id",
        "artifact_name",
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


def validate_activation(
    root: Path,
    current_commit: str,
    *,
    event_name: str,
    pull_request: int | None,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the exact review branch or its one merge-commit activation."""

    root = root.resolve()
    current = _resolve(root, current_commit)
    base = contract["activation"]["base_commit"]
    release = contract["release"]["commit"]
    _verify_release_identity(root, contract)
    if _git(
        root,
        ["merge-base", "--is-ancestor", base, current],
        operation="activation ancestry inspection",
        check=False,
    ).returncode != 0:
        raise ValidationRecoveryError("Activation is not based on the reviewed source")
    if _git(
        root,
        ["merge-base", "--is-ancestor", release, current],
        operation="unsynchronized release inspection",
        check=False,
    ).returncode == 0:
        raise ValidationRecoveryError("Activation unexpectedly contains the release ledger")

    if event_name == "pull_request":
        if pull_request != contract["activation"]["pull_request"]:
            raise ValidationRecoveryError("Recovery is restricted to activation PR #53")
        feature_head = current
        activation_commit = None
    elif event_name in {"push", "workflow_dispatch"}:
        parents = _parents(root, current)
        if len(parents) != 2 or parents[0] != base:
            raise ValidationRecoveryError(
                "Recovery activation is not a direct two-parent merge onto its source"
            )
        feature_head = parents[1]
        if _tree(root, feature_head) != _tree(root, current):
            raise ValidationRecoveryError("Activation merge tree differs from its reviewed head")
        activation_commit = current
    else:
        raise ValidationRecoveryError("Event cannot activate published-release recovery")

    if _git(
        root,
        ["merge-base", "--is-ancestor", base, feature_head],
        operation="feature ancestry inspection",
        check=False,
    ).returncode != 0:
        raise ValidationRecoveryError("Activation feature head has invalid ancestry")
    changed = _changed_paths(root, base, feature_head)
    if changed != ACTIVATION_PATHS:
        raise ValidationRecoveryError("Activation tree delta is outside the reviewed scope")
    if _changed_paths(root, base, current) != ACTIVATION_PATHS:
        raise ValidationRecoveryError("Current activation tree delta changed")
    blobs = []
    for path in ACTIVATION_PATHS:
        object_id, content = _blob(root, current, path)
        blobs.append(
            {
                "git_blob_sha": object_id,
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return {
        "activation_commit": activation_commit,
        "activation_tree": _tree(root, current),
        "base_commit": base,
        "feature_head": feature_head,
        "paths": blobs,
        "pull_request": contract["activation"]["pull_request"],
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

    activation = validate_activation(
        root,
        current_commit,
        event_name=event_name,
        pull_request=pull_request,
        contract=contract,
    )
    release = contract["release"]["commit"]
    merge_tree = _merge_tree(root, current_commit, release)
    synthetic = _synthetic_commit(root, merge_tree, current_commit, release)
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
        "activation": activation,
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
        "schema_version": SCHEMA_VERSION,
        "source_commit": current_commit,
        "synthetic_merge_commit": synthetic,
        "synthetic_merge_tree": merge_tree,
    }


def validate_hold(
    root: Path,
    current_commit: str,
    *,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Hold automatic new-release creation across activation and sync merge."""

    current = _resolve(root, current_commit)
    try:
        activation = validate_activation(
            root,
            current,
            event_name="workflow_dispatch",
            pull_request=None,
            contract=contract,
        )
        state = "activation-pending-sync"
        activation_commit = current
    except ValidationRecoveryError:
        parents = _parents(root, current)
        release = contract["release"]["commit"]
        if len(parents) != 2 or parents[1] != release:
            raise ValidationRecoveryError("Current main is outside the recovery hold")
        activation_commit = parents[0]
        activation = validate_activation(
            root,
            activation_commit,
            event_name="workflow_dispatch",
            pull_request=None,
            contract=contract,
        )
        if _tree(root, current) != _merge_tree(root, activation_commit, release):
            raise ValidationRecoveryError("Synchronization merge tree changed")
        state = "synchronized-awaiting-retirement"
    return {
        "activation": activation,
        "activation_commit": activation_commit,
        "current_commit": current,
        "recovery_id": contract["recovery_id"],
        "schema_version": SCHEMA_VERSION,
        "state": state,
    }


def apply_harness(
    root: Path,
    harness_commit: str,
    *,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Overlay only the reviewed fixture corrections onto exact v0.6.2."""

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
    activation = validate_activation(
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
    if changed or actual != HARNESS_PATHS:
        raise ValidationRecoveryError("Harness changed files outside its reviewed scope")
    return {
        "activation": activation,
        "files": files,
        "harness_commit": harness_commit,
        "recovery_id": contract["recovery_id"],
        "release_commit": release,
        "release_tree": contract["release"]["tree"],
        "schema_version": SCHEMA_VERSION,
    }


def create_attestation(
    root: Path,
    activation_commit: str,
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
    activation = validate_activation(
        root,
        activation_commit,
        event_name="workflow_dispatch",
        pull_request=None,
        contract=contract,
    )
    files = []
    for path in HARNESS_PATHS:
        object_id, content = _blob(root, activation_commit, path)
        files.append(
            {
                "git_blob_sha": object_id,
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return {
        "activation": activation,
        "harness": {
            "commit": activation_commit,
            "files": files,
            "tree": activation["activation_tree"],
        },
        "recovery_id": contract["recovery_id"],
        "release": dict(contract["release"]),
        "repository": repository,
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "validation": {
            "actor": actor,
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": activation_commit,
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
    expected_activation: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "activation",
        "harness",
        "recovery_id",
        "release",
        "repository",
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
        activation.get("base_commit") != contract["activation"]["base_commit"]
        or activation.get("pull_request") != contract["activation"]["pull_request"]
        or COMMIT_RE.fullmatch(str(activation.get("activation_tree"))) is None
        or COMMIT_RE.fullmatch(str(activation.get("feature_head"))) is None
        or expected_activation is not None
        and activation_commit != expected_activation
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

    harness = value.get("harness")
    if not isinstance(harness, dict) or set(harness) != {"commit", "files", "tree"}:
        raise ValidationRecoveryError("Validation recovery harness is invalid")
    if (
        harness.get("commit") != activation_commit
        or harness.get("tree") != activation.get("activation_tree")
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
    activation_by_path = {item["path"]: item for item in paths}
    if any(activation_by_path[item["path"]] != item for item in files):
        raise ValidationRecoveryError("Harness bytes differ from the activation commit")

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
        or validation.get("head_sha") != activation_commit
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
    expected_activation: str | None = None,
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
        expected_activation=expected_activation,
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
    for name in ("activation", "ledger", "hold"):
        command = subparsers.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--source-commit", required=True)
        command.add_argument("--output", type=Path, required=True)
        if name in {"activation", "ledger"}:
            command.add_argument(
                "--event-name",
                choices=("pull_request", "push", "workflow_dispatch"),
                required=True,
            )
            command.add_argument("--pull-request", type=int)
    overlay = subparsers.add_parser("apply-harness")
    overlay.add_argument("--root", type=Path, required=True)
    overlay.add_argument("--harness-commit", required=True)
    overlay.add_argument("--output", type=Path, required=True)
    attest = subparsers.add_parser("attest")
    attest.add_argument("--root", type=Path, required=True)
    attest.add_argument("--activation-commit", required=True)
    attest.add_argument("--repository", required=True)
    attest.add_argument("--run-id", type=int, required=True)
    attest.add_argument("--run-attempt", type=int, required=True)
    attest.add_argument("--actor", required=True)
    attest.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify-attestation")
    verify.add_argument("--attestation", type=Path, required=True)
    verify.add_argument("--run-id", type=int, required=True)
    verify.add_argument("--run-attempt", type=int, required=True)
    verify.add_argument("--activation-commit", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        contract = load_contract(arguments.contract)
        if arguments.command == "activation":
            report = validate_activation(
                arguments.root,
                arguments.source_commit,
                event_name=arguments.event_name,
                pull_request=arguments.pull_request,
                contract=contract,
            )
            _write(arguments.output, report)
            _append_outputs(
                {
                    "activation_commit": report["activation_commit"] or "feature",
                    "feature_head": report["feature_head"],
                    "release_commit": contract["release"]["commit"],
                    "source_commit": contract["release"]["source_commit"],
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
                arguments.activation_commit,
                repository=arguments.repository,
                run_id=arguments.run_id,
                run_attempt=arguments.run_attempt,
                actor=arguments.actor,
                contract=contract,
            )
            _write(arguments.output, report)
            _append_outputs(
                {
                    "activation_commit": arguments.activation_commit,
                    "release_commit": contract["release"]["commit"],
                    "source_commit": contract["release"]["source_commit"],
                }
            )
        else:
            load_attestation(
                arguments.attestation,
                contract=contract,
                expected_run_id=arguments.run_id,
                expected_run_attempt=arguments.run_attempt,
                expected_activation=arguments.activation_commit,
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
