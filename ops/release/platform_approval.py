#!/usr/bin/env python3
"""Bind one in-ChatGPT approval to one preflighted platform release.

The release workflow writes a machine-readable readiness comment only after the
exact sync pull request, Source CI run, immutable release ref, and production
preflight agree.  The merge workflow accepts only the matching owner-authored
approval marker embedded in a two-parent merge commit and re-verifies all remote
evidence before production deploy.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from open_sync_pr import GitHubClient, SyncConfig, SyncPrError
import validation_recovery


SCHEMA_VERSION = 2
RECOVERY_SCHEMA_VERSION = 3
FEATURE_READINESS_SCHEMA_VERSION = 1
PREFLIGHT_EVIDENCE_SCHEMA_VERSION = 1
READY_PREFIX = "<!-- buh-platform-ready:v2 "
APPROVAL_PREFIX = "<!-- buh-chatgpt-approved:v2 "
RECOVERY_READY_PREFIX = "<!-- buh-platform-ready-recovery:v1 "
RECOVERY_APPROVAL_PREFIX = "<!-- buh-chatgpt-approved-recovery:v1 "
MARKER_SUFFIX = " -->"
BOT_LOGIN = "github-actions[bot]"
MAX_PAGES = 100
SOURCE_CI_POLL_SECONDS = 15
SOURCE_CI_POLLS = 160
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class ApprovalError(SyncPrError):
    """A fail-closed approval error safe to print."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _safe_int(name: str, value: Any, *, maximum: int = 10**20) -> int:
    if type(value) is not int or value < 1 or value > maximum:
        raise ApprovalError(f"{name} is invalid")
    return value


def _timestamp(name: str, value: Any) -> dt.datetime:
    if not isinstance(value, str) or TIMESTAMP_RE.fullmatch(value) is None:
        raise ApprovalError(f"{name} is invalid")
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc
    )


def _login(value: Any) -> str | None:
    return value.get("login") if isinstance(value, dict) else None


def _repo_name(value: Any) -> str | None:
    return value.get("full_name") if isinstance(value, dict) else None


def _path(repository: str, suffix: str) -> str:
    return f"/repos/{repository}/{suffix}"


def _config(
    *,
    owner: str,
    repository: str,
    version: str,
    source_commit: str,
    release_commit: str,
    api_url: str,
    server_url: str,
    output: Path,
) -> SyncConfig:
    config = SyncConfig.validated(
        owner=owner,
        repository=repository,
        version=version,
        release_branch=f"release/platform-v{version}",
        sync_branch=f"sync/platform-v{version}",
        source_sha=source_commit,
        release_commit=release_commit,
        api_url=api_url,
        server_url=server_url,
        output=output,
    )
    if COMMIT_RE.fullmatch(source_commit) is None or COMMIT_RE.fullmatch(
        release_commit
    ) is None:
        raise ApprovalError("GitHub release approvals require 40-character commits")
    return config


def _read_ref(client: GitHubClient, config: SyncConfig, branch: str) -> str:
    response = client.get(_path(config.repository, f"git/ref/heads/{branch}"))
    obj = response.get("object") if isinstance(response, dict) else None
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if (
        not isinstance(response, dict)
        or response.get("ref") != f"refs/heads/{branch}"
        or not isinstance(obj, dict)
        or obj.get("type") != "commit"
        or not isinstance(sha, str)
        or COMMIT_RE.fullmatch(sha) is None
    ):
        raise ApprovalError(f"GitHub returned a malformed {branch} ref")
    return sha


def _read_commit(client: GitHubClient, config: SyncConfig, commit: str) -> dict[str, Any]:
    response = client.get(_path(config.repository, f"commits/{commit}"))
    if not isinstance(response, dict) or response.get("sha") != commit:
        raise ApprovalError("GitHub returned a malformed commit")
    return response


def _verify_release(client: GitHubClient, config: SyncConfig, *, sync: bool) -> None:
    if _read_ref(client, config, config.release_branch) != config.release_commit:
        raise ApprovalError("Immutable release ref no longer matches the release commit")
    if sync and _read_ref(client, config, config.sync_branch) != config.release_commit:
        raise ApprovalError("Sync ref no longer matches the release commit")
    release = _read_commit(client, config, config.release_commit)
    parents = release.get("parents")
    if (
        not isinstance(parents, list)
        or len(parents) != 1
        or not isinstance(parents[0], dict)
        or parents[0].get("sha") != config.source_sha
    ):
        raise ApprovalError("Release commit is not a direct child of the tested source")


def _validate_pr(
    pr: Any,
    config: SyncConfig,
    number: int,
    *,
    state: str,
) -> dict[str, Any]:
    if not isinstance(pr, dict):
        raise ApprovalError("GitHub returned a malformed synchronization PR")
    head = pr.get("head")
    base = pr.get("base")
    expected_url = f"{config.server_url}/{config.repository}/pull/{number}"
    if (
        pr.get("number") != number
        or pr.get("state") != state
        or pr.get("draft") is not False
        or pr.get("title") != f"Sync platform release v{config.version}"
        or pr.get("html_url") != expected_url
        or _login(pr.get("user")) != config.owner
        or not isinstance(head, dict)
        or head.get("ref") != config.sync_branch
        or head.get("sha") != config.release_commit
        or _repo_name(head.get("repo")) != config.repository
        or not isinstance(base, dict)
        or base.get("ref") != "main"
        or _repo_name(base.get("repo")) != config.repository
    ):
        raise ApprovalError("Synchronization PR identity does not match the release")
    return pr


def _get_pr(client: GitHubClient, config: SyncConfig, number: int, state: str):
    return _validate_pr(
        client.get(_path(config.repository, f"pulls/{number}")),
        config,
        number,
        state=state,
    )


def _pages(
    client: GitHubClient,
    path: str,
    *,
    query: Mapping[str, str] | None = None,
) -> list[Any]:
    values: list[Any] = []
    for page in range(1, MAX_PAGES + 1):
        payload = client.get(
            path,
            {**(query or {}), "per_page": "100", "page": str(page)},
        )
        if not isinstance(payload, list):
            raise ApprovalError("GitHub returned a malformed paginated response")
        values.extend(payload)
        if len(payload) < 100:
            return values
    raise ApprovalError("GitHub pagination exceeded its fixed limit")


def _workflow_path(run: Mapping[str, Any]) -> str | None:
    value = run.get("path")
    return value.split("@", 1)[0] if isinstance(value, str) else None


def _run_pr_numbers(run: Mapping[str, Any]) -> set[int]:
    pulls = run.get("pull_requests")
    if not isinstance(pulls, list):
        return set()
    return {
        item["number"]
        for item in pulls
        if isinstance(item, dict)
        and type(item.get("number")) is int
        and item["number"] > 0
    }


def _source_ci_run(
    client: GitHubClient,
    config: SyncConfig,
    number: int,
    *,
    sleeper: Callable[[float], None],
    polls: int = SOURCE_CI_POLLS,
) -> dict[str, Any]:
    for poll in range(polls):
        payload = client.get(
            _path(config.repository, "actions/workflows/source-ci.yml/runs"),
            {
                "event": "pull_request",
                "head_sha": config.release_commit,
                "per_page": "100",
            },
        )
        runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
        if not isinstance(runs, list):
            raise ApprovalError("GitHub returned malformed Source CI runs")
        exact = [
            run
            for run in runs
            if isinstance(run, dict)
            and run.get("event") == "pull_request"
            and run.get("head_sha") == config.release_commit
            and run.get("head_branch") == config.sync_branch
            and _workflow_path(run) == ".github/workflows/source-ci.yml"
            and _repo_name(run.get("head_repository")) == config.repository
            and _repo_name(run.get("repository")) == config.repository
            and number in _run_pr_numbers(run)
            and type(run.get("id")) is int
        ]
        if exact:
            selected = max(
                exact,
                key=lambda run: (
                    int(run.get("run_number", 0)),
                    int(run.get("run_attempt", 0)),
                    run["id"],
                ),
            )
            if selected.get("status") == "completed":
                if selected.get("conclusion") != "success":
                    raise ApprovalError("Exact sync PR Source CI did not pass")
                _safe_int(
                    "Source CI run attempt",
                    selected.get("run_attempt"),
                    maximum=10**6,
                )
                return selected
        if poll + 1 < polls:
            sleeper(SOURCE_CI_POLL_SECONDS)
    raise ApprovalError("Exact sync PR Source CI did not complete before the timeout")


def _workflow_run(
    client: GitHubClient,
    config: SyncConfig,
    run_id: int,
    run_attempt: int,
    *,
    completed: bool,
) -> dict[str, Any]:
    run = client.get(_path(config.repository, f"actions/runs/{run_id}"))
    if (
        not isinstance(run, dict)
        or run.get("id") != run_id
        or run.get("run_attempt") != run_attempt
        or run.get("event") != "workflow_run"
        or run.get("head_sha") != config.source_sha
        or run.get("head_branch") != "main"
        or _workflow_path(run) != ".github/workflows/auto-platform-release.yml"
        or _repo_name(run.get("repository")) != config.repository
        or _repo_name(run.get("head_repository")) != config.repository
    ):
        raise ApprovalError("Automatic release workflow identity is invalid")
    if completed:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise ApprovalError("Automatic release workflow did not complete successfully")
    elif run.get("status") not in {"in_progress", "queued"}:
        raise ApprovalError("Automatic release workflow is not active")
    return run


def _verify_main_run(
    client: GitHubClient,
    config: SyncConfig,
    run_id: int,
    attempt: int,
) -> None:
    run = client.get(_path(config.repository, f"actions/runs/{run_id}"))
    if (
        not isinstance(run, dict)
        or run.get("id") != run_id
        or run.get("run_attempt") != attempt
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("event") != "push"
        or run.get("head_sha") != config.source_sha
        or run.get("head_branch") != "main"
        or _workflow_path(run) != ".github/workflows/source-ci.yml"
        or _repo_name(run.get("repository")) != config.repository
        or _repo_name(run.get("head_repository")) != config.repository
    ):
        raise ApprovalError("Recorded main Validate PR evidence is invalid")


def _allowed_operator(login: str | None, config: SyncConfig, work_actor: str) -> bool:
    if work_actor and LOGIN_RE.fullmatch(work_actor) is None:
        raise ApprovalError("Configured ChatGPT Work actor is invalid")
    return login == config.owner or bool(work_actor and login == work_actor)


def _commit_tree(commit: Mapping[str, Any], *, context: str) -> str:
    data = commit.get("commit")
    tree = data.get("tree") if isinstance(data, dict) else None
    value = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(value, str) or COMMIT_RE.fullmatch(value) is None:
        raise ApprovalError(f"{context} tree identity is invalid")
    return value


def _main_ci_run(
    client: GitHubClient,
    config: SyncConfig,
    commit: str,
) -> dict[str, Any]:
    payload = client.get(
        _path(config.repository, "actions/workflows/source-ci.yml/runs"),
        {"event": "push", "head_sha": commit, "per_page": "100"},
    )
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    total = payload.get("total_count") if isinstance(payload, dict) else None
    if not isinstance(runs, list) or type(total) is not int or total != len(runs):
        raise ApprovalError("Activation Validate PR run list is malformed")
    exact = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("event") == "push"
        and run.get("head_sha") == commit
        and run.get("head_branch") == "main"
        and _workflow_path(run) == ".github/workflows/source-ci.yml"
        and _repo_name(run.get("head_repository")) == config.repository
        and _repo_name(run.get("repository")) == config.repository
        and type(run.get("id")) is int
    ]
    if not exact:
        raise ApprovalError("Exact activation Validate PR run is unavailable")
    selected = max(
        exact,
        key=lambda run: (
            int(run.get("run_number", 0)),
            int(run.get("run_attempt", 0)),
            run["id"],
        ),
    )
    if selected.get("status") != "completed" or selected.get("conclusion") != "success":
        raise ApprovalError("Newest exact activation Validate PR run did not pass")
    _safe_int(
        "Activation Validate PR run attempt",
        selected.get("run_attempt"),
        maximum=10**6,
    )
    return selected


def _verify_main_run_for_commit(
    client: GitHubClient,
    config: SyncConfig,
    commit: str,
    run_id: int,
    attempt: int,
) -> None:
    run = client.get(_path(config.repository, f"actions/runs/{run_id}"))
    if (
        not isinstance(run, dict)
        or run.get("id") != run_id
        or run.get("run_attempt") != attempt
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("event") != "push"
        or run.get("head_sha") != commit
        or run.get("head_branch") != "main"
        or _workflow_path(run) != ".github/workflows/source-ci.yml"
        or _repo_name(run.get("repository")) != config.repository
        or _repo_name(run.get("head_repository")) != config.repository
    ):
        raise ApprovalError("Recorded activation Validate PR evidence is invalid")


def _verify_activation_remote(
    client: GitHubClient,
    config: SyncConfig,
    attestation: Mapping[str, Any],
    *,
    state: str,
    work_actor: str,
) -> dict[str, Any]:
    activation = attestation["activation"]
    activation_commit = activation["activation_commit"]
    feature_head = activation["feature_head"]
    commit = _read_commit(client, config, activation_commit)
    parents = commit.get("parents")
    if (
        not isinstance(parents, list)
        or len(parents) != 2
        or not all(isinstance(parent, dict) for parent in parents)
        or parents[0].get("sha") != config.source_sha
        or parents[1].get("sha") != feature_head
        or _commit_tree(commit, context="Recovery activation")
        != activation["activation_tree"]
    ):
        raise ApprovalError("Recovery activation merge identity is invalid")
    feature = _read_commit(client, config, feature_head)
    if _commit_tree(feature, context="Recovery activation feature") != activation[
        "activation_tree"
    ]:
        raise ApprovalError("Recovery activation tree differs from its feature head")
    pulls = _pages(
        client,
        _path(config.repository, f"commits/{activation_commit}/pulls"),
    )
    matches = [
        pull
        for pull in pulls
        if isinstance(pull, dict)
        and pull.get("number") == activation["pull_request"]
    ]
    if len(matches) != 1:
        raise ApprovalError("Recovery activation lacks one exact pull request")
    pull = matches[0]
    head = pull.get("head")
    base = pull.get("base")
    if (
        pull.get("state") != state
        or pull.get("draft") is not False
        or pull.get("merge_commit_sha") != activation_commit
        or pull.get("merged_at") is None
        or not _allowed_operator(_login(pull.get("merged_by")), config, work_actor)
        or not _allowed_operator(_login(pull.get("user")), config, work_actor)
        or not isinstance(head, dict)
        or head.get("sha") != feature_head
        or _repo_name(head.get("repo")) != config.repository
        or not isinstance(base, dict)
        or base.get("ref") != "main"
        or _repo_name(base.get("repo")) != config.repository
    ):
        raise ApprovalError("Recovery activation pull-request identity is invalid")
    _timestamp("Recovery activation merge timestamp", pull.get("merged_at"))
    return pull


def _recovery_workflow_run(
    client: GitHubClient,
    config: SyncConfig,
    run_id: int,
    run_attempt: int,
    activation_commit: str,
    *,
    completed: bool,
    work_actor: str,
) -> dict[str, Any]:
    run = client.get(_path(config.repository, f"actions/runs/{run_id}"))
    if (
        not isinstance(run, dict)
        or run.get("id") != run_id
        or run.get("run_attempt") != run_attempt
        or run_attempt != 1
        or run.get("event") != "workflow_dispatch"
        or run.get("head_sha") != activation_commit
        or run.get("head_branch") != "main"
        or _workflow_path(run) != validation_recovery.WORKFLOW_PATH
        or _repo_name(run.get("repository")) != config.repository
        or _repo_name(run.get("head_repository")) != config.repository
        or not _allowed_operator(_login(run.get("actor")), config, work_actor)
        or not _allowed_operator(
            _login(run.get("triggering_actor")), config, work_actor
        )
    ):
        raise ApprovalError("Published-release recovery workflow identity is invalid")
    if completed:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise ApprovalError(
                "Published-release recovery workflow did not complete successfully"
            )
    elif run.get("status") not in {"in_progress", "queued"}:
        raise ApprovalError("Published-release recovery workflow is not active")
    return run


def _verify_newest_recovery_run(
    client: GitHubClient,
    config: SyncConfig,
    activation_commit: str,
    run_id: int,
    run_attempt: int,
    *,
    completed: bool,
    work_actor: str,
) -> None:
    payload = client.get(
        _path(config.repository, f"actions/workflows/{validation_recovery.WORKFLOW_PATH.rsplit('/', 1)[-1]}/runs"),
        {
            "event": "workflow_dispatch",
            "head_sha": activation_commit,
            "per_page": "100",
        },
    )
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    total = payload.get("total_count") if isinstance(payload, dict) else None
    if not isinstance(runs, list) or type(total) is not int or total != len(runs):
        raise ApprovalError("Published-release recovery run list is malformed")
    exact = []
    for run in runs:
        if not isinstance(run, dict):
            raise ApprovalError("Published-release recovery run list is malformed")
        if (
            run.get("event") != "workflow_dispatch"
            or run.get("head_sha") != activation_commit
            or run.get("head_branch") != "main"
            or _workflow_path(run) != validation_recovery.WORKFLOW_PATH
            or _repo_name(run.get("repository")) != config.repository
            or _repo_name(run.get("head_repository")) != config.repository
            or type(run.get("id")) is not int
            or not _allowed_operator(_login(run.get("actor")), config, work_actor)
            or not _allowed_operator(
                _login(run.get("triggering_actor")), config, work_actor
            )
        ):
            raise ApprovalError("Published-release recovery run provenance is invalid")
        _safe_int(
            "Published-release recovery run attempt",
            run.get("run_attempt"),
            maximum=10**6,
        )
        exact.append(run)
    if not exact:
        raise ApprovalError("Published-release recovery run is unavailable")
    selected = max(
        exact,
        key=lambda run: (
            int(run.get("run_number", 0)),
            int(run.get("run_attempt", 0)),
            run["id"],
        ),
    )
    if selected.get("id") != run_id or selected.get("run_attempt") != run_attempt:
        raise ApprovalError("Recorded published-release recovery run is not newest")
    expected_status = "completed" if completed else None
    if completed and (
        selected.get("status") != expected_status
        or selected.get("conclusion") != "success"
    ):
        raise ApprovalError("Newest published-release recovery run did not pass")
    if not completed and selected.get("status") not in {"in_progress", "queued"}:
        raise ApprovalError("Newest published-release recovery run is not active")


def _verify_recovery_preflight(
    client: GitHubClient,
    config: SyncConfig,
    contract: Mapping[str, Any],
    *,
    work_actor: str,
) -> None:
    expected = contract["preflight"]
    run_id = expected["run_id"]
    run = client.get(_path(config.repository, f"actions/runs/{run_id}"))
    if (
        not isinstance(run, dict)
        or run.get("id") != run_id
        or run.get("run_attempt") != expected["run_attempt"]
        or run.get("status") != "completed"
        or run.get("conclusion") != "failure"
        or run.get("event") != "workflow_run"
        or run.get("head_sha") != config.source_sha
        or run.get("head_branch") != "main"
        or _workflow_path(run) != ".github/workflows/auto-platform-release.yml"
        or _repo_name(run.get("repository")) != config.repository
        or _repo_name(run.get("head_repository")) != config.repository
        or not _allowed_operator(_login(run.get("actor")), config, work_actor)
        or not _allowed_operator(
            _login(run.get("triggering_actor")), config, work_actor
        )
    ):
        raise ApprovalError("Retained recovery preflight workflow identity is invalid")
    payload = client.get(
        _path(config.repository, f"actions/runs/{run_id}/jobs"),
        {"per_page": "100"},
    )
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    total = payload.get("total_count") if isinstance(payload, dict) else None
    if not isinstance(jobs, list) or type(total) is not int or total != len(jobs):
        raise ApprovalError("Retained recovery release jobs are malformed")
    normalized = []
    for job in jobs:
        if (
            not isinstance(job, dict)
            or job.get("status") != "completed"
            or job.get("run_id") != run_id
            or job.get("run_attempt") != expected["run_attempt"]
            or job.get("head_sha") != config.source_sha
            or job.get("workflow_name") != "Prepare Release"
        ):
            raise ApprovalError("Retained recovery release job identity is invalid")
        normalized.append(
            {
                "conclusion": job.get("conclusion"),
                "id": job.get("id"),
                "name": job.get("name"),
            }
        )
    if normalized != expected["jobs"]:
        raise ApprovalError("Retained recovery release job results changed")


def _artifact(
    client: GitHubClient,
    config: SyncConfig,
    run_id: int,
    name: str,
    *,
    head_sha: str | None = None,
    expected_id: int | None = None,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    expected_head = config.source_sha if head_sha is None else head_sha
    payload = client.get(
        _path(config.repository, f"actions/runs/{run_id}/artifacts"),
        {"name": name, "per_page": "100"},
    )
    artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
    total_count = payload.get("total_count") if isinstance(payload, dict) else None
    if (
        not isinstance(artifacts, list)
        or type(total_count) is not int
        or total_count != len(artifacts)
    ):
        raise ApprovalError("Exact retained preflight artifact is unavailable")
    exact = [item for item in artifacts if isinstance(item, dict) and item.get("name") == name]
    if len(exact) != 1:
        raise ApprovalError("Exact retained preflight artifact is unavailable")
    artifact = exact[0]
    artifact_id = _safe_int("Preflight artifact ID", artifact.get("id"))
    size = artifact.get("size_in_bytes")
    digest = artifact.get("digest")
    workflow_run = artifact.get("workflow_run")
    if (
        artifact.get("expired") is not False
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 < size <= 40 * 1024 * 1024
        or not isinstance(digest, str)
        or DIGEST_RE.fullmatch(digest) is None
        or not isinstance(workflow_run, dict)
        or workflow_run.get("id") != run_id
        or workflow_run.get("head_sha") != expected_head
        or workflow_run.get("head_branch") != "main"
    ):
        raise ApprovalError("Exact retained preflight artifact is unavailable")
    if expected_id is not None and artifact_id != expected_id:
        raise ApprovalError("Retained preflight artifact ID changed")
    if expected_digest is not None and digest != expected_digest:
        raise ApprovalError("Retained preflight artifact digest changed")
    return artifact


def _feature_readiness(
    value: Any,
    config: SyncConfig,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "repository",
        "attestation_mode",
        "feature_pr",
        "merger",
        "merge_source_commit",
        "feature_validation",
        "review_digest",
        "readiness",
        "preview_required",
        "preview",
    }:
        raise ApprovalError("Feature-readiness evidence schema is invalid")
    if (
        value.get("schema_version") != FEATURE_READINESS_SCHEMA_VERSION
        or value.get("repository") != config.repository
        or value.get("merge_source_commit") != config.source_sha
    ):
        raise ApprovalError("Feature-readiness evidence does not match the release")
    mode = value.get("attestation_mode")
    if mode not in {"premerge-published", "first-introduction-postmerge"}:
        raise ApprovalError("Feature-readiness attestation mode is invalid")
    merger = value.get("merger")
    if not isinstance(merger, str) or LOGIN_RE.fullmatch(merger) is None:
        raise ApprovalError("Feature-readiness merger identity is invalid")
    feature_pr = value.get("feature_pr")
    if not isinstance(feature_pr, dict) or set(feature_pr) != {"number", "head_sha"}:
        raise ApprovalError("Feature-readiness pull-request identity is invalid")
    feature_number = _safe_int(
        "Feature pull request number", feature_pr.get("number"), maximum=10**9
    )
    if mode == "first-introduction-postmerge" and feature_number != 44:
        raise ApprovalError("First-introduction readiness is restricted to pull request 44")
    feature_head = feature_pr.get("head_sha")
    if (
        not isinstance(feature_head, str)
        or COMMIT_RE.fullmatch(feature_head) is None
        or feature_head == config.source_sha
    ):
        raise ApprovalError("Feature-readiness pull-request head is invalid")
    validation = value.get("feature_validation")
    if not isinstance(validation, dict) or set(validation) != {
        "check_run_id",
        "workflow_run_id",
        "workflow_run_attempt",
    }:
        raise ApprovalError("Feature validation evidence schema is invalid")
    normalized_validation = {
        "check_run_id": _safe_int(
            "Feature validation check run ID", validation.get("check_run_id")
        ),
        "workflow_run_id": _safe_int(
            "Feature validation workflow run ID", validation.get("workflow_run_id")
        ),
        "workflow_run_attempt": _safe_int(
            "Feature validation workflow run attempt",
            validation.get("workflow_run_attempt"),
            maximum=10**6,
        ),
    }
    review_digest = value.get("review_digest")
    if not isinstance(review_digest, str) or DIGEST_RE.fullmatch(review_digest) is None:
        raise ApprovalError("Feature-readiness review digest is invalid")

    readiness = value.get("readiness")
    normalized_readiness = None
    if mode == "premerge-published":
        if not isinstance(readiness, dict) or set(readiness) != {
            "workflow_run_id",
            "workflow_run_attempt",
            "artifact_id",
            "artifact_name",
            "artifact_digest",
            "review_digest",
        }:
            raise ApprovalError("Published readiness evidence schema is invalid")
        expected_name = (
            f"pr-readiness-{feature_number}-{feature_head}-"
            f"{review_digest.removeprefix('sha256:')}"
        )
        if readiness.get("artifact_name") != expected_name:
            raise ApprovalError("Published readiness artifact name is invalid")
        if readiness.get("review_digest") != review_digest:
            raise ApprovalError("Published readiness review digest does not match")
        artifact_digest = readiness.get("artifact_digest")
        if not isinstance(artifact_digest, str) or DIGEST_RE.fullmatch(
            artifact_digest
        ) is None:
            raise ApprovalError("Published readiness artifact digest is invalid")
        normalized_readiness = {
            "workflow_run_id": _safe_int(
                "Readiness workflow run ID", readiness.get("workflow_run_id")
            ),
            "workflow_run_attempt": _safe_int(
                "Readiness workflow run attempt",
                readiness.get("workflow_run_attempt"),
                maximum=10**6,
            ),
            "artifact_id": _safe_int(
                "Readiness artifact ID", readiness.get("artifact_id")
            ),
            "artifact_name": expected_name,
            "artifact_digest": artifact_digest,
            "review_digest": review_digest,
        }
    elif readiness is not None:
        raise ApprovalError("First-introduction readiness evidence must be null")

    preview_required = value.get("preview_required")
    if not isinstance(preview_required, bool):
        raise ApprovalError("Feature preview requirement is invalid")
    preview = value.get("preview")
    normalized_preview = None
    if preview_required:
        if not isinstance(preview, dict) or set(preview) != {
            "run_id",
            "run_attempt",
            "manifest_artifact_id",
            "manifest_artifact_digest",
            "evidence_artifact_id",
            "evidence_artifact_digest",
        }:
            raise ApprovalError("Feature preview evidence schema is invalid")
        manifest_digest = preview.get("manifest_artifact_digest")
        evidence_digest = preview.get("evidence_artifact_digest")
        if (
            not isinstance(manifest_digest, str)
            or DIGEST_RE.fullmatch(manifest_digest) is None
            or not isinstance(evidence_digest, str)
            or DIGEST_RE.fullmatch(evidence_digest) is None
        ):
            raise ApprovalError("Feature preview artifact digest is invalid")
        normalized_preview = {
            "run_id": _safe_int("Feature preview run ID", preview.get("run_id")),
            "run_attempt": _safe_int(
                "Feature preview run attempt",
                preview.get("run_attempt"),
                maximum=10**6,
            ),
            "manifest_artifact_id": _safe_int(
                "Feature preview manifest artifact ID",
                preview.get("manifest_artifact_id"),
            ),
            "manifest_artifact_digest": manifest_digest,
            "evidence_artifact_id": _safe_int(
                "Feature preview evidence artifact ID",
                preview.get("evidence_artifact_id"),
            ),
            "evidence_artifact_digest": evidence_digest,
        }
    elif preview is not None:
        raise ApprovalError("Unexpected feature preview evidence is present")
    return {
        "schema_version": FEATURE_READINESS_SCHEMA_VERSION,
        "repository": config.repository,
        "attestation_mode": mode,
        "merger": merger,
        "feature_pr": {"number": feature_number, "head_sha": feature_head},
        "merge_source_commit": config.source_sha,
        "feature_validation": normalized_validation,
        "review_digest": review_digest,
        "readiness": normalized_readiness,
        "preview_required": preview_required,
        "preview": normalized_preview,
    }


def _nonce(fields: Mapping[str, Any]) -> str:
    bound = {key: value for key, value in fields.items() if key != "approval_nonce"}
    return hashlib.sha256((_canonical(bound) + "\n").encode("ascii")).hexdigest()


def approval_payload(ready: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "approval_nonce": ready["approval_nonce"],
        "platform_version": ready["platform_version"],
        "release_commit": ready["release_commit"],
        "schema_version": ready["schema_version"],
    }


def marker(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}{_canonical(payload)}{MARKER_SUFFIX}"


def _marker_payload(body: Any, prefix: str) -> dict[str, Any] | None:
    if not isinstance(body, str) or prefix not in body:
        return None
    if body.count(prefix) != 1:
        raise ApprovalError("A workflow marker appears more than once in one comment")
    start = body.index(prefix) + len(prefix)
    end = body.find(MARKER_SUFFIX, start)
    if end < 0 or "\n" in body[start:end] or "\r" in body[start:end]:
        raise ApprovalError("A workflow marker is malformed")
    raw = body[start:end]
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ApprovalError("A workflow marker contains invalid JSON") from exc
    if not isinstance(payload, dict) or _canonical(payload) != raw:
        raise ApprovalError("A workflow marker is not canonical")
    return payload


def _comments(
    client: GitHubClient, config: SyncConfig, number: int
) -> list[dict[str, Any]]:
    values = _pages(client, _path(config.repository, f"issues/{number}/comments"))
    if not all(isinstance(item, dict) for item in values):
        raise ApprovalError("GitHub returned malformed pull-request comments")
    return values


def _post_ready_comment(
    client: GitHubClient,
    config: SyncConfig,
    number: int,
    ready: Mapping[str, Any],
    *,
    recovery: bool = False,
) -> dict[str, Any]:
    ready_prefix = RECOVERY_READY_PREFIX if recovery else READY_PREFIX
    approval_prefix = RECOVERY_APPROVAL_PREFIX if recovery else APPROVAL_PREFIX
    approval = marker(approval_prefix, approval_payload(ready))
    explanation = (
        "The reviewed published-release recovery validation and retained "
        "no-change production preflight passed for this exact immutable "
        "release. Production has not been changed.\n\n"
        if recovery
        else "Source CI and the no-change production preflight passed for this "
        "exact immutable release. Production has not been changed.\n\n"
    )
    body = (
        f"{marker(ready_prefix, ready)}\n\n"
        "### Ready for the one production approval in ChatGPT\n\n"
        f"{explanation}"
        "After the repository owner explicitly approves this release in ChatGPT, "
        "ChatGPT must place this exact record in the merge commit message and "
        "perform one merge-commit action:\n\n"
        f"```html\n{approval}\n```"
    )
    response = client.post(
        _path(config.repository, f"issues/{number}/comments"),
        {"body": body},
        context="readiness comment publication",
    )
    if (
        not isinstance(response, dict)
        or response.get("body") != body
        or _login(response.get("user")) != BOT_LOGIN
        or type(response.get("id")) is not int
    ):
        raise ApprovalError("GitHub did not return the exact readiness comment")
    return response


def ready(
    config: SyncConfig,
    token: str,
    *,
    number: int,
    feature_readiness: Mapping[str, Any],
    main_validation_run_id: int,
    main_validation_run_attempt: int,
    manifest_sha256: str,
    preflight_run_id: int,
    preflight_run_attempt: int,
    preflight_artifact: str,
    transport=None,
    sleeper: Callable[[float], None] = time.sleep,
    polls: int = SOURCE_CI_POLLS,
) -> dict[str, Any]:
    if NONCE_RE.fullmatch(manifest_sha256) is None:
        raise ApprovalError("Preflight manifest SHA-256 is invalid")
    expected_artifact = (
        f"platform-v2-preflight-{preflight_run_id}-{preflight_run_attempt}"
    )
    if preflight_artifact != expected_artifact:
        raise ApprovalError("Preflight artifact name is not bound to its workflow run")
    client = GitHubClient(
        config.api_url, token, transport=transport, sleeper=sleeper
    )
    published = _feature_readiness(feature_readiness, config)
    main_run_id = _safe_int("Main Validate PR run ID", main_validation_run_id)
    main_run_attempt = _safe_int(
        "Main Validate PR run attempt",
        main_validation_run_attempt,
        maximum=10**6,
    )
    preflight_id = _safe_int("Preflight run ID", preflight_run_id)
    preflight_attempt = _safe_int(
        "Preflight run attempt", preflight_run_attempt, maximum=10**6
    )
    _get_pr(client, config, number, "open")
    if _read_ref(client, config, "main") != config.source_sha:
        raise ApprovalError(
            "Current main advanced before production readiness could be published"
        )
    _verify_release(client, config, sync=True)
    _verify_main_run(
        client,
        config,
        main_run_id,
        main_run_attempt,
    )
    _workflow_run(
        client,
        config,
        preflight_id,
        preflight_attempt,
        completed=False,
    )
    artifact = _artifact(client, config, preflight_id, preflight_artifact)
    source_run = _source_ci_run(
        client, config, number, sleeper=sleeper, polls=polls
    )
    _verify_newest_source_run(
        client,
        config,
        number,
        source_run["id"],
        source_run["run_attempt"],
    )
    # Source CI can take long enough for main, release refs, or retained
    # preflight evidence to change while this job is polling.  Take one final
    # live snapshot immediately before publishing the human approval request.
    if _read_ref(client, config, "main") != config.source_sha:
        raise ApprovalError(
            "Current main advanced before production readiness could be published"
        )
    _verify_release(client, config, sync=True)
    _workflow_run(
        client,
        config,
        preflight_id,
        preflight_attempt,
        completed=False,
    )
    artifact = _artifact(
        client,
        config,
        preflight_id,
        preflight_artifact,
        expected_id=artifact["id"],
        expected_digest=artifact["digest"],
    )
    existing = [
        item
        for item in _comments(client, config, number)
        if _marker_payload(item.get("body"), READY_PREFIX) is not None
    ]
    if existing:
        raise ApprovalError("A readiness marker already exists for this pull request")
    fields: dict[str, Any] = {
        "feature_readiness": published,
        "main_validation_run_attempt": main_run_attempt,
        "main_validation_run_id": main_run_id,
        "manifest_sha256": manifest_sha256,
        "platform_version": config.version,
        "preflight_artifact": preflight_artifact,
        "preflight_artifact_digest": artifact["digest"],
        "preflight_artifact_id": artifact["id"],
        "preflight_run_attempt": preflight_attempt,
        "preflight_run_id": preflight_id,
        "pull_request": number,
        "release_commit": config.release_commit,
        "repository": config.repository,
        "schema_version": SCHEMA_VERSION,
        "source_commit": config.source_sha,
        "sync_validation_run_attempt": source_run["run_attempt"],
        "sync_validation_run_id": source_run["id"],
    }
    fields["approval_nonce"] = _nonce(fields)
    comment = _post_ready_comment(client, config, number, fields)
    return {
        "approval_marker": marker(APPROVAL_PREFIX, approval_payload(fields)),
        "comment_id": comment["id"],
        "ready": fields,
        "schema_version": SCHEMA_VERSION,
    }


def ready_recovery(
    contract: Mapping[str, Any],
    token: str,
    *,
    owner: str,
    repository: str,
    api_url: str,
    server_url: str,
    output: Path,
    feature_readiness: Mapping[str, Any],
    validation_attestation: Mapping[str, Any],
    validation_artifact_id: int,
    validation_artifact_name: str,
    validation_artifact_digest: str,
    work_actor: str = "",
    transport=None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Publish readiness for the one exact, already-published v0.6.2."""

    release = contract["release"]
    sync = contract["sync"]
    preflight = contract["preflight"]
    config = _config(
        owner=owner,
        repository=repository,
        version=contract["platform_version"],
        source_commit=release["source_commit"],
        release_commit=release["commit"],
        api_url=api_url,
        server_url=server_url,
        output=output,
    )
    if repository != contract["repository"]:
        raise ApprovalError("Recovery repository does not match its contract")
    published = _feature_readiness(feature_readiness, config)
    attestation = validation_recovery.validate_attestation_data(
        validation_attestation,
        contract=contract,
    )
    validation = attestation["validation"]
    validation_run_id = _safe_int(
        "Recovery validation run ID", validation.get("run_id")
    )
    validation_attempt = _safe_int(
        "Recovery validation run attempt",
        validation.get("run_attempt"),
        maximum=10**6,
    )
    activation_commit = attestation["activation"]["activation_commit"]
    expected_validation_artifact = (
        f"platform-validation-recovery-v{contract['platform_version']}-"
        f"{release['commit'][:12]}-{validation_run_id}-{validation_attempt}"
    )
    artifact_id = _safe_int(
        "Recovery validation artifact ID", validation_artifact_id
    )
    if validation_artifact_name != expected_validation_artifact:
        raise ApprovalError("Recovery validation artifact name is invalid")
    if DIGEST_RE.fullmatch(validation_artifact_digest) is None:
        raise ApprovalError("Recovery validation artifact digest is invalid")

    client = GitHubClient(
        config.api_url, token, transport=transport, sleeper=sleeper
    )
    _get_pr(client, config, sync["pull_request"], "open")
    if _read_ref(client, config, "main") != activation_commit:
        raise ApprovalError("Recovery activation is not the current main tip")
    _verify_release(client, config, sync=True)
    _verify_activation_remote(
        client,
        config,
        attestation,
        state="closed",
        work_actor=work_actor,
    )
    activation_run = _main_ci_run(client, config, activation_commit)
    _verify_main_run(
        client,
        config,
        contract["main_validation"]["run_id"],
        contract["main_validation"]["run_attempt"],
    )
    _verify_recovery_preflight(
        client, config, contract, work_actor=work_actor
    )
    preflight_artifact = _artifact(
        client,
        config,
        preflight["run_id"],
        preflight["artifact_name"],
        expected_id=preflight["artifact_id"],
        expected_digest=preflight["artifact_digest"],
    )
    _recovery_workflow_run(
        client,
        config,
        validation_run_id,
        validation_attempt,
        activation_commit,
        completed=False,
        work_actor=work_actor,
    )
    _verify_newest_recovery_run(
        client,
        config,
        activation_commit,
        validation_run_id,
        validation_attempt,
        completed=False,
        work_actor=work_actor,
    )
    validation_artifact = _artifact(
        client,
        config,
        validation_run_id,
        validation_artifact_name,
        head_sha=activation_commit,
        expected_id=artifact_id,
        expected_digest=validation_artifact_digest,
    )

    # Repeat every mutable boundary immediately before posting the one approval
    # request. The original failed run is accepted only through its exact job set.
    if _read_ref(client, config, "main") != activation_commit:
        raise ApprovalError("Recovery activation moved before readiness publication")
    _verify_release(client, config, sync=True)
    _verify_main_run_for_commit(
        client,
        config,
        activation_commit,
        activation_run["id"],
        activation_run["run_attempt"],
    )
    _verify_recovery_preflight(
        client, config, contract, work_actor=work_actor
    )
    preflight_artifact = _artifact(
        client,
        config,
        preflight["run_id"],
        preflight["artifact_name"],
        expected_id=preflight_artifact["id"],
        expected_digest=preflight_artifact["digest"],
    )
    _recovery_workflow_run(
        client,
        config,
        validation_run_id,
        validation_attempt,
        activation_commit,
        completed=False,
        work_actor=work_actor,
    )
    validation_artifact = _artifact(
        client,
        config,
        validation_run_id,
        validation_artifact_name,
        head_sha=activation_commit,
        expected_id=validation_artifact["id"],
        expected_digest=validation_artifact["digest"],
    )
    existing = []
    for item in _comments(client, config, sync["pull_request"]):
        normal = _marker_payload(item.get("body"), READY_PREFIX)
        recovered = _marker_payload(item.get("body"), RECOVERY_READY_PREFIX)
        if normal is not None or recovered is not None:
            existing.append(item)
    if existing:
        raise ApprovalError("A readiness marker already exists for this pull request")

    fields: dict[str, Any] = {
        "feature_readiness": published,
        "main_validation_run_attempt": contract["main_validation"]["run_attempt"],
        "main_validation_run_id": contract["main_validation"]["run_id"],
        "manifest_sha256": release["manifest_sha256"],
        "platform_version": contract["platform_version"],
        "preflight_artifact": preflight["artifact_name"],
        "preflight_artifact_digest": preflight_artifact["digest"],
        "preflight_artifact_id": preflight_artifact["id"],
        "preflight_run_attempt": preflight["run_attempt"],
        "preflight_run_id": preflight["run_id"],
        "pull_request": sync["pull_request"],
        "recovery_validation": {
            "activation_validation_run_attempt": activation_run["run_attempt"],
            "activation_validation_run_id": activation_run["id"],
            "artifact_digest": validation_artifact["digest"],
            "artifact_id": validation_artifact["id"],
            "artifact_name": validation_artifact_name,
            "attestation": attestation,
            "mode": "published-release-recovery",
        },
        "release_commit": release["commit"],
        "repository": contract["repository"],
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "source_commit": release["source_commit"],
        # Compatibility aliases keep the existing guarded deploy input surface;
        # recovery_validation records their distinct, truthful meaning.
        "sync_validation_run_attempt": validation_attempt,
        "sync_validation_run_id": validation_run_id,
    }
    fields["approval_nonce"] = _nonce(fields)
    comment = _post_ready_comment(
        client,
        config,
        sync["pull_request"],
        fields,
        recovery=True,
    )
    return {
        "approval_marker": marker(
            RECOVERY_APPROVAL_PREFIX, approval_payload(fields)
        ),
        "comment_id": comment["id"],
        "ready": fields,
        "schema_version": RECOVERY_SCHEMA_VERSION,
    }


READY_KEYS = {
    "approval_nonce",
    "feature_readiness",
    "main_validation_run_attempt",
    "main_validation_run_id",
    "manifest_sha256",
    "platform_version",
    "preflight_artifact",
    "preflight_artifact_digest",
    "preflight_artifact_id",
    "preflight_run_attempt",
    "preflight_run_id",
    "pull_request",
    "release_commit",
    "repository",
    "schema_version",
    "source_commit",
    "sync_validation_run_attempt",
    "sync_validation_run_id",
}
RECOVERY_READY_KEYS = READY_KEYS | {"recovery_validation"}


def _validate_ready_payload(payload: Mapping[str, Any], config: SyncConfig, number: int):
    if payload.get("schema_version") == RECOVERY_SCHEMA_VERSION:
        return _validate_recovery_ready_payload(payload, config, number)
    if set(payload) != READY_KEYS or payload.get("schema_version") != SCHEMA_VERSION:
        raise ApprovalError("Readiness marker schema is invalid")
    if (
        payload.get("repository") != config.repository
        or payload.get("pull_request") != number
        or payload.get("platform_version") != config.version
        or payload.get("source_commit") != config.source_sha
        or payload.get("release_commit") != config.release_commit
        or not isinstance(payload.get("manifest_sha256"), str)
        or NONCE_RE.fullmatch(payload["manifest_sha256"]) is None
        or not isinstance(payload.get("approval_nonce"), str)
        or NONCE_RE.fullmatch(payload["approval_nonce"]) is None
    ):
        raise ApprovalError("Readiness marker does not match the release")
    feature_readiness = _feature_readiness(payload.get("feature_readiness"), config)
    main_run_id = _safe_int(
        "Main Validate PR run ID", payload.get("main_validation_run_id")
    )
    main_attempt = _safe_int(
        "Main Validate PR run attempt",
        payload.get("main_validation_run_attempt"),
        maximum=10**6,
    )
    run_id = _safe_int("Preflight run ID", payload.get("preflight_run_id"))
    run_attempt = _safe_int(
        "Preflight run attempt", payload.get("preflight_run_attempt"), maximum=10**6
    )
    sync_run_id = _safe_int(
        "Sync Validate PR run ID", payload.get("sync_validation_run_id")
    )
    sync_attempt = _safe_int(
        "Sync Validate PR run attempt",
        payload.get("sync_validation_run_attempt"),
        maximum=10**6,
    )
    artifact_id = _safe_int(
        "Preflight artifact ID", payload.get("preflight_artifact_id")
    )
    artifact_digest = payload.get("preflight_artifact_digest")
    if not isinstance(artifact_digest, str) or DIGEST_RE.fullmatch(
        artifact_digest
    ) is None:
        raise ApprovalError("Readiness marker artifact digest is invalid")
    if payload.get("preflight_artifact") != f"platform-v2-preflight-{run_id}-{run_attempt}":
        raise ApprovalError("Readiness marker artifact identity is invalid")
    if payload["approval_nonce"] != _nonce(payload):
        raise ApprovalError("Readiness marker nonce is invalid")
    return {
        "feature_readiness": feature_readiness,
        "main_run_id": main_run_id,
        "main_attempt": main_attempt,
        "mode": "source-ci",
        "preflight_run_id": run_id,
        "preflight_attempt": run_attempt,
        "preflight_artifact_id": artifact_id,
        "preflight_artifact_digest": artifact_digest,
        "sync_run_id": sync_run_id,
        "sync_attempt": sync_attempt,
    }


def _validate_recovery_ready_payload(
    payload: Mapping[str, Any], config: SyncConfig, number: int
) -> dict[str, Any]:
    if set(payload) != RECOVERY_READY_KEYS:
        raise ApprovalError("Recovery readiness marker schema is invalid")
    if (
        payload.get("repository") != config.repository
        or payload.get("pull_request") != number
        or payload.get("platform_version") != config.version
        or payload.get("source_commit") != config.source_sha
        or payload.get("release_commit") != config.release_commit
        or payload.get("manifest_sha256")
        != validation_recovery.load_contract()["release"]["manifest_sha256"]
        or not isinstance(payload.get("approval_nonce"), str)
        or NONCE_RE.fullmatch(payload["approval_nonce"]) is None
    ):
        raise ApprovalError("Recovery readiness marker does not match the release")
    contract = validation_recovery.load_contract()
    if number != contract["sync"]["pull_request"]:
        raise ApprovalError("Recovery readiness targets the wrong synchronization PR")
    feature_readiness = _feature_readiness(payload.get("feature_readiness"), config)
    main_run_id = _safe_int(
        "Main Validate PR run ID", payload.get("main_validation_run_id")
    )
    main_attempt = _safe_int(
        "Main Validate PR run attempt",
        payload.get("main_validation_run_attempt"),
        maximum=10**6,
    )
    if (
        main_run_id != contract["main_validation"]["run_id"]
        or main_attempt != contract["main_validation"]["run_attempt"]
    ):
        raise ApprovalError("Recovery main validation evidence changed")
    preflight_id = _safe_int(
        "Preflight run ID", payload.get("preflight_run_id")
    )
    preflight_attempt = _safe_int(
        "Preflight run attempt",
        payload.get("preflight_run_attempt"),
        maximum=10**6,
    )
    preflight_artifact_id = _safe_int(
        "Preflight artifact ID", payload.get("preflight_artifact_id")
    )
    if (
        preflight_id != contract["preflight"]["run_id"]
        or preflight_attempt != contract["preflight"]["run_attempt"]
        or payload.get("preflight_artifact")
        != contract["preflight"]["artifact_name"]
        or preflight_artifact_id != contract["preflight"]["artifact_id"]
        or payload.get("preflight_artifact_digest")
        != contract["preflight"]["artifact_digest"]
    ):
        raise ApprovalError("Recovery preflight evidence changed")
    recovery = payload.get("recovery_validation")
    if not isinstance(recovery, dict) or set(recovery) != {
        "activation_validation_run_attempt",
        "activation_validation_run_id",
        "artifact_digest",
        "artifact_id",
        "artifact_name",
        "attestation",
        "mode",
    }:
        raise ApprovalError("Recovery validation evidence schema is invalid")
    if recovery.get("mode") != "published-release-recovery":
        raise ApprovalError("Recovery validation mode is invalid")
    attestation = validation_recovery.validate_attestation_data(
        recovery.get("attestation"), contract=contract
    )
    validation_run_id = _safe_int(
        "Recovery validation run ID", payload.get("sync_validation_run_id")
    )
    validation_attempt = _safe_int(
        "Recovery validation run attempt",
        payload.get("sync_validation_run_attempt"),
        maximum=10**6,
    )
    if (
        attestation["validation"]["run_id"] != validation_run_id
        or attestation["validation"]["run_attempt"] != validation_attempt
    ):
        raise ApprovalError("Recovery workflow identity aliases changed")
    artifact_id = _safe_int(
        "Recovery validation artifact ID", recovery.get("artifact_id")
    )
    artifact_digest = recovery.get("artifact_digest")
    if not isinstance(artifact_digest, str) or DIGEST_RE.fullmatch(
        artifact_digest
    ) is None:
        raise ApprovalError("Recovery validation artifact digest is invalid")
    expected_name = (
        f"platform-validation-recovery-v{config.version}-"
        f"{config.release_commit[:12]}-{validation_run_id}-{validation_attempt}"
    )
    if recovery.get("artifact_name") != expected_name:
        raise ApprovalError("Recovery validation artifact name is invalid")
    activation_run_id = _safe_int(
        "Activation Validate PR run ID",
        recovery.get("activation_validation_run_id"),
    )
    activation_attempt = _safe_int(
        "Activation Validate PR run attempt",
        recovery.get("activation_validation_run_attempt"),
        maximum=10**6,
    )
    if payload["approval_nonce"] != _nonce(payload):
        raise ApprovalError("Recovery readiness marker nonce is invalid")
    return {
        "activation_attempt": activation_attempt,
        "activation_commit": attestation["activation"]["activation_commit"],
        "activation_run_id": activation_run_id,
        "artifact_digest": artifact_digest,
        "artifact_id": artifact_id,
        "artifact_name": expected_name,
        "attestation": attestation,
        "feature_readiness": feature_readiness,
        "main_attempt": main_attempt,
        "main_run_id": main_run_id,
        "mode": "published-release-recovery",
        "preflight_artifact_digest": contract["preflight"]["artifact_digest"],
        "preflight_artifact_id": preflight_artifact_id,
        "preflight_attempt": preflight_attempt,
        "preflight_run_id": preflight_id,
        "sync_attempt": validation_attempt,
        "sync_run_id": validation_run_id,
    }


def _verify_source_run(
    client: GitHubClient,
    config: SyncConfig,
    number: int,
    run_id: int,
    attempt: int,
) -> None:
    run = client.get(_path(config.repository, f"actions/runs/{run_id}"))
    if (
        not isinstance(run, dict)
        or run.get("id") != run_id
        or run.get("run_attempt") != attempt
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("event") != "pull_request"
        or run.get("head_sha") != config.release_commit
        or run.get("head_branch") != config.sync_branch
        or _workflow_path(run) != ".github/workflows/source-ci.yml"
        or _repo_name(run.get("head_repository")) != config.repository
        or _repo_name(run.get("repository")) != config.repository
    ):
        raise ApprovalError("Recorded sync PR Source CI evidence is invalid")
    if number in _run_pr_numbers(run):
        return
    if run.get("pull_requests") != []:
        raise ApprovalError("Recorded Source CI pull-request association is invalid")

    # GitHub can clear a run's pull_requests array after merge. Readiness already
    # bound this exact run/attempt to the open PR, and authorize() has checked the
    # approval-bearing merge. Use the durable commit-to-PR association only here;
    # never relax readiness or accept a missing, malformed, or conflicting list.
    pulls = _pages(
        client, _path(config.repository, f"commits/{config.release_commit}/pulls")
    )
    matches = [
        pr for pr in pulls
        if isinstance(pr, dict) and pr.get("number") == number
    ]
    if len(matches) != 1:
        raise ApprovalError("Source CI release lacks one exact merged PR association")
    merged_pr = _validate_pr(matches[0], config, number, state="closed")
    _timestamp("Source CI associated PR merge timestamp", merged_pr.get("merged_at"))


def _verify_newest_source_run(
    client: GitHubClient,
    config: SyncConfig,
    number: int,
    run_id: int,
    attempt: int,
) -> None:
    """Reject a superseded sync-PR run even when its head SHA is unchanged."""

    payload = client.get(
        _path(config.repository, "actions/workflows/source-ci.yml/runs"),
        {
            "event": "pull_request",
            "head_sha": config.release_commit,
            "per_page": "100",
        },
    )
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    total_count = payload.get("total_count") if isinstance(payload, dict) else None
    if (
        not isinstance(runs, list)
        or type(total_count) is not int
        or total_count != len(runs)
    ):
        raise ApprovalError("Sync PR Source CI run list is incomplete or malformed")

    exact: list[dict[str, Any]] = []
    for item in runs:
        if not isinstance(item, dict):
            raise ApprovalError("Sync PR Source CI run list is malformed")
        if (
            item.get("event") != "pull_request"
            or item.get("head_sha") != config.release_commit
            or item.get("head_branch") != config.sync_branch
            or _workflow_path(item) != ".github/workflows/source-ci.yml"
            or _repo_name(item.get("head_repository")) != config.repository
            or _repo_name(item.get("repository")) != config.repository
            or type(item.get("id")) is not int
            or item["id"] < 1
        ):
            raise ApprovalError("Sync PR Source CI run-list provenance is invalid")
        _safe_int(
            "Sync PR Source CI run attempt",
            item.get("run_attempt"),
            maximum=10**6,
        )
        associations = item.get("pull_requests")
        if associations != [] and (
            not isinstance(associations, list)
            or len(associations) != 1
            or _run_pr_numbers(item) != {number}
        ):
            raise ApprovalError("Sync PR Source CI run-list association is invalid")
        exact.append(item)

    if not exact:
        raise ApprovalError("Exact sync PR Source CI run is unavailable")
    selected = max(
        exact,
        key=lambda run: (
            int(run.get("run_number", 0)),
            int(run.get("run_attempt", 0)),
            run["id"],
        ),
    )
    if selected.get("id") != run_id or selected.get("run_attempt") != attempt:
        raise ApprovalError("Recorded sync PR Source CI is not the newest exact-head run")
    if selected.get("status") != "completed" or selected.get("conclusion") != "success":
        raise ApprovalError("Newest exact sync PR Source CI did not pass")


def _verify_merge_commit(
    client: GitHubClient,
    config: SyncConfig,
    merge_commit: str,
    *,
    first_parent: str | None = None,
) -> dict[str, Any]:
    expected_first_parent = config.source_sha if first_parent is None else first_parent
    merge = _read_commit(client, config, merge_commit)
    parents = merge.get("parents")
    if (
        not isinstance(parents, list)
        or len(parents) != 2
        or not all(isinstance(item, dict) for item in parents)
        or parents[0].get("sha") != expected_first_parent
        or parents[1].get("sha") != config.release_commit
    ):
        raise ApprovalError(
            "Synchronization PR was not merged with a merge commit directly "
            "onto its tested source"
        )
    if _read_ref(client, config, "main") != merge_commit:
        raise ApprovalError("Synchronization merge is no longer the current main tip")
    return merge


def authorize(
    event: Mapping[str, Any],
    *,
    owner: str,
    repository: str,
    actor: str,
    triggering_actor: str,
    run_attempt: int,
    api_url: str,
    server_url: str,
    output: Path,
    token: str,
    work_actor: str = "",
    transport=None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        raise ApprovalError("Event does not contain a pull request")
    number = _safe_int("Pull request number", event.get("number"))
    title = pr.get("title")
    head = pr.get("head")
    if (
        event.get("action") != "closed"
        or pr.get("merged") is not True
        or pr.get("state") != "closed"
        or pr.get("number") != number
        or actor != owner
        or triggering_actor != owner
        or run_attempt != 1
        or _login(event.get("sender")) != owner
        or _login(pr.get("merged_by")) != owner
        or _login(pr.get("user")) != owner
        or not isinstance(title, str)
        or not isinstance(head, dict)
    ):
        raise ApprovalError("Merged pull-request event is not owner-authorized")
    match = re.fullmatch(r"Sync platform release v(.+)", title)
    if match is None or VERSION_RE.fullmatch(match.group(1)) is None:
        raise ApprovalError("Merged pull-request title is not an exact platform release")
    version = match.group(1)
    source_commit = None
    release_commit = head.get("sha")
    if not isinstance(release_commit, str) or COMMIT_RE.fullmatch(release_commit) is None:
        raise ApprovalError("Merged synchronization head is not a full commit")
    # Read the exact release commit once to discover the tested source, then
    # validate every remaining field through the shared release contract.
    provisional = _config(
        owner=owner,
        repository=repository,
        version=version,
        source_commit="0" * 40,
        release_commit=release_commit,
        api_url=api_url,
        server_url=server_url,
        output=output,
    )
    client = GitHubClient(api_url, token, transport=transport, sleeper=sleeper)
    release = _read_commit(client, provisional, release_commit)
    parents = release.get("parents")
    if (
        not isinstance(parents, list)
        or len(parents) != 1
        or not isinstance(parents[0], dict)
    ):
        raise ApprovalError("Release commit has invalid parentage")
    source_commit = parents[0].get("sha")
    if not isinstance(source_commit, str) or COMMIT_RE.fullmatch(source_commit) is None:
        raise ApprovalError("Release source commit is invalid")
    config = _config(
        owner=owner,
        repository=repository,
        version=version,
        source_commit=source_commit,
        release_commit=release_commit,
        api_url=api_url,
        server_url=server_url,
        output=output,
    )
    _validate_pr(pr, config, number, state="closed")
    if pr.get("merged_at") is None:
        raise ApprovalError("Merged synchronization PR has no merge timestamp")
    merged_at = _timestamp("PR merge timestamp", pr["merged_at"])
    _verify_release(client, config, sync=False)
    merge_commit = pr.get("merge_commit_sha")
    if not isinstance(merge_commit, str) or COMMIT_RE.fullmatch(merge_commit) is None:
        raise ApprovalError("Synchronization merge commit is invalid")

    comments = _comments(client, config, number)
    ready_comments = []
    for comment in comments:
        normal = _marker_payload(comment.get("body"), READY_PREFIX)
        recovered = _marker_payload(comment.get("body"), RECOVERY_READY_PREFIX)
        if normal is not None and recovered is not None:
            raise ApprovalError("One comment contains conflicting readiness markers")
        if normal is not None:
            ready_comments.append((comment, normal, False))
        if recovered is not None:
            ready_comments.append((comment, recovered, True))
    if len(ready_comments) != 1:
        raise ApprovalError(
            "Exactly one GitHub Actions readiness marker is required"
        )
    ready_comment, ready_payload, recovered = ready_comments[0]
    ready_comment_id = _safe_int(
        "Readiness comment ID", ready_comment.get("id")
    )
    if _login(ready_comment.get("user")) != BOT_LOGIN:
        raise ApprovalError("Readiness marker was not created by GitHub Actions")
    ready_at = _timestamp("Readiness comment timestamp", ready_comment.get("created_at"))
    if ready_at > merged_at:
        raise ApprovalError("Synchronization PR was merged before readiness")
    evidence = _validate_ready_payload(ready_payload, config, number)
    if recovered != (evidence["mode"] == "published-release-recovery"):
        raise ApprovalError("Readiness marker prefix and evidence mode disagree")
    first_parent = (
        evidence["activation_commit"] if recovered else config.source_sha
    )
    merge = _verify_merge_commit(
        client,
        config,
        merge_commit,
        first_parent=first_parent,
    )
    commit_data = merge.get("commit")
    commit_message = (
        commit_data.get("message") if isinstance(commit_data, dict) else None
    )
    approval_prefix = RECOVERY_APPROVAL_PREFIX if recovered else APPROVAL_PREFIX
    approved = _marker_payload(commit_message, approval_prefix)
    if approved is None:
        raise ApprovalError("Merge commit lacks the ChatGPT approval marker")
    if set(approved) != {
        "approval_nonce",
        "platform_version",
        "release_commit",
        "schema_version",
    } or approved != approval_payload(ready_payload):
        raise ApprovalError("ChatGPT approval marker does not match readiness evidence")
    _verify_main_run(
        client,
        config,
        evidence["main_run_id"],
        evidence["main_attempt"],
    )
    recovery_artifact = None
    if recovered:
        contract = validation_recovery.load_contract()
        activation_pull = _verify_activation_remote(
            client,
            config,
            evidence["attestation"],
            state="closed",
            work_actor=work_actor,
        )
        activation_merged_at = _timestamp(
            "Recovery activation merge timestamp", activation_pull.get("merged_at")
        )
        if activation_merged_at >= merged_at:
            raise ApprovalError(
                "Synchronization PR did not merge after recovery activation"
            )
        _verify_main_run_for_commit(
            client,
            config,
            evidence["activation_commit"],
            evidence["activation_run_id"],
            evidence["activation_attempt"],
        )
        _verify_recovery_preflight(
            client, config, contract, work_actor=work_actor
        )
        _recovery_workflow_run(
            client,
            config,
            evidence["sync_run_id"],
            evidence["sync_attempt"],
            evidence["activation_commit"],
            completed=True,
            work_actor=work_actor,
        )
        _verify_newest_recovery_run(
            client,
            config,
            evidence["activation_commit"],
            evidence["sync_run_id"],
            evidence["sync_attempt"],
            completed=True,
            work_actor=work_actor,
        )
        recovery_artifact = _artifact(
            client,
            config,
            evidence["sync_run_id"],
            evidence["artifact_name"],
            head_sha=evidence["activation_commit"],
            expected_id=evidence["artifact_id"],
            expected_digest=evidence["artifact_digest"],
        )
    else:
        _workflow_run(
            client,
            config,
            evidence["preflight_run_id"],
            evidence["preflight_attempt"],
            completed=True,
        )
        _verify_source_run(
            client,
            config,
            number,
            evidence["sync_run_id"],
            evidence["sync_attempt"],
        )
        _verify_newest_source_run(
            client,
            config,
            number,
            evidence["sync_run_id"],
            evidence["sync_attempt"],
        )
    artifact = _artifact(
        client,
        config,
        evidence["preflight_run_id"],
        ready_payload["preflight_artifact"],
        expected_id=evidence["preflight_artifact_id"],
        expected_digest=evidence["preflight_artifact_digest"],
    )
    # Authorization may spend time paging comments and checking workflows.  A
    # newer main commit or deleted artifact while this run was queued must not
    # survive the last boundary read. Re-pin the immutable release, current
    # main tip, and exact retained artifact before returning authorization.
    _verify_release(client, config, sync=False)
    if _read_ref(client, config, "main") != merge_commit:
        raise ApprovalError("Synchronization merge is no longer the current main tip")
    final_ready_comments = []
    for comment in _comments(client, config, number):
        normal = _marker_payload(comment.get("body"), READY_PREFIX)
        recovery = _marker_payload(comment.get("body"), RECOVERY_READY_PREFIX)
        if normal is not None and recovery is not None:
            raise ApprovalError("One live comment contains conflicting readiness markers")
        if normal is not None:
            final_ready_comments.append((comment, normal, False))
        if recovery is not None:
            final_ready_comments.append((comment, recovery, True))
    if len(final_ready_comments) != 1:
        raise ApprovalError(
            "Exactly one live GitHub Actions readiness marker is required"
        )
    final_ready_comment, final_ready_payload, final_recovered = final_ready_comments[0]
    if (
        _safe_int("Readiness comment ID", final_ready_comment.get("id"))
        != ready_comment_id
        or _login(final_ready_comment.get("user")) != BOT_LOGIN
        or final_ready_payload != ready_payload
        or final_recovered != recovered
        or _timestamp(
            "Readiness comment timestamp", final_ready_comment.get("created_at")
        )
        != ready_at
    ):
        raise ApprovalError("Live readiness approval changed during authorization")
    artifact = _artifact(
        client,
        config,
        evidence["preflight_run_id"],
        ready_payload["preflight_artifact"],
        expected_id=artifact["id"],
        expected_digest=artifact["digest"],
    )
    if recovered:
        contract = validation_recovery.load_contract()
        _verify_recovery_preflight(
            client, config, contract, work_actor=work_actor
        )
        _verify_main_run_for_commit(
            client,
            config,
            evidence["activation_commit"],
            evidence["activation_run_id"],
            evidence["activation_attempt"],
        )
        _recovery_workflow_run(
            client,
            config,
            evidence["sync_run_id"],
            evidence["sync_attempt"],
            evidence["activation_commit"],
            completed=True,
            work_actor=work_actor,
        )
        recovery_artifact = _artifact(
            client,
            config,
            evidence["sync_run_id"],
            evidence["artifact_name"],
            head_sha=evidence["activation_commit"],
            expected_id=recovery_artifact["id"],
            expected_digest=recovery_artifact["digest"],
        )
    report = {
        "approval_record": "merge-commit",
        "approval_nonce": ready_payload["approval_nonce"],
        "feature_readiness": evidence["feature_readiness"],
        "feature_pr_number": evidence["feature_readiness"]["feature_pr"]["number"],
        "feature_head_sha": evidence["feature_readiness"]["feature_pr"]["head_sha"],
        "main_validation_run_attempt": evidence["main_attempt"],
        "main_validation_run_id": evidence["main_run_id"],
        "manifest_sha256": ready_payload["manifest_sha256"],
        "merge_commit": merge_commit,
        "platform_version": version,
        "preflight_artifact": ready_payload["preflight_artifact"],
        "preflight_artifact_digest": artifact["digest"],
        "preflight_artifact_id": artifact["id"],
        "preflight_run_attempt": evidence["preflight_attempt"],
        "preflight_run_id": evidence["preflight_run_id"],
        "pull_request": number,
        "release_commit": release_commit,
        "repository": repository,
        "schema_version": SCHEMA_VERSION,
        "source_commit": source_commit,
        "sync_validation_run_attempt": evidence["sync_attempt"],
        "sync_validation_run_id": evidence["sync_run_id"],
    }
    if recovered:
        report["validation_mode"] = "published-release-recovery"
        report["recovery_validation"] = ready_payload["recovery_validation"]
    return report


def verify_artifact(report: Mapping[str, Any], root: Path) -> None:
    if not isinstance(report, dict) or report.get("schema_version") != SCHEMA_VERSION:
        raise ApprovalError("Approval report is invalid")
    expected = {
        "manifest_sha256": report.get("manifest_sha256"),
        "platform_version": report.get("platform_version"),
        "release_commit": report.get("release_commit"),
        "repository": report.get("repository"),
        "source_commit": report.get("source_commit"),
    }
    for name, value in expected.items():
        if not isinstance(value, str) or not value:
            raise ApprovalError(f"Approval report {name} is invalid")
    run_id = _safe_int("Preflight run ID", report.get("preflight_run_id"))
    attempt = _safe_int(
        "Preflight run attempt", report.get("preflight_run_attempt"), maximum=10**6
    )
    _safe_int("Preflight artifact ID", report.get("preflight_artifact_id"))
    artifact_digest = report.get("preflight_artifact_digest")
    if not isinstance(artifact_digest, str) or DIGEST_RE.fullmatch(
        artifact_digest
    ) is None:
        raise ApprovalError("Approval report preflight artifact digest is invalid")
    if not root.is_dir() or root.is_symlink():
        raise ApprovalError("Downloaded preflight artifact directory is unsafe")
    total = 0
    for path in root.rglob("*"):
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode):
            raise ApprovalError("Downloaded preflight artifact contains a symlink")
        if stat.S_ISDIR(details.st_mode):
            continue
        if not stat.S_ISREG(details.st_mode) or details.st_size > 16 * 1024 * 1024:
            raise ApprovalError("Downloaded preflight artifact contains an unsafe file")
        total += details.st_size
    if total <= 0 or total > 32 * 1024 * 1024:
        raise ApprovalError("Downloaded preflight artifact has an unsafe size")
    receiver_path = root / "receiver.txt"
    attempt_path = root / "attempt.txt"
    try:
        receiver = json.loads(receiver_path.read_text(encoding="utf-8"))
        lines = attempt_path.read_text(encoding="utf-8").splitlines()
        attempt_report = json.loads(lines[1]) if len(lines) == 2 else None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ApprovalError("Downloaded preflight evidence is unreadable") from exc
    receiver_recovery = (
        receiver.get("recovery_transition_sha256")
        if isinstance(receiver, dict)
        else None
    )
    receiver_without_recovery = dict(receiver) if isinstance(receiver, dict) else {}
    receiver_without_recovery.pop("recovery_transition_sha256", None)
    if receiver_without_recovery != {
        "manifest_sha256": expected["manifest_sha256"],
        "platform_version": expected["platform_version"],
        "result": "preflight-passed",
        "schema_version": PREFLIGHT_EVIDENCE_SCHEMA_VERSION,
    }:
        raise ApprovalError("Receiver evidence does not prove the exact preflight")
    if (
        lines[0] != "B-UH Platform v2 guarded attempt report"
        or not isinstance(attempt_report, dict)
        or attempt_report.get("schema_version") != PREFLIGHT_EVIDENCE_SCHEMA_VERSION
        or attempt_report.get("attempt_id") != f"gh-{run_id}-{attempt}"
        or attempt_report.get("operation") != "preflight"
        or attempt_report.get("result") != "success"
        or attempt_report.get("repository") != expected["repository"]
        or attempt_report.get("source_commit") != expected["source_commit"]
        or attempt_report.get("release_commit") != expected["release_commit"]
        or attempt_report.get("platform_version") != expected["platform_version"]
        or attempt_report.get("manifest_sha256") != expected["manifest_sha256"]
    ):
        raise ApprovalError("Observer evidence does not prove the exact preflight")
    release_recovery = attempt_report.get("release_recovery")
    if release_recovery is None:
        if receiver_recovery is not None:
            raise ApprovalError("Preflight recovery evidence is incomplete")
    elif (
        not isinstance(release_recovery, dict)
        or release_recovery.get("sha256") != receiver_recovery
        or not isinstance(receiver_recovery, str)
        or NONCE_RE.fullmatch(receiver_recovery) is None
    ):
        raise ApprovalError("Preflight recovery evidence does not agree")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (_canonical(value) + "\n").encode("ascii")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ApprovalError("Could not write the approval report") from exc


def _append_outputs(path: str | None, values: Mapping[str, Any]) -> None:
    if not path:
        return
    safe = re.compile(r"^[\x20-\x7e]+$")
    selected = {
        key: str(values[key])
        for key in (
            "feature_head_sha",
            "feature_pr_number",
            "main_validation_run_attempt",
            "main_validation_run_id",
            "manifest_sha256",
            "platform_version",
            "preflight_artifact",
            "preflight_artifact_digest",
            "preflight_artifact_id",
            "preflight_run_attempt",
            "preflight_run_id",
            "pull_request",
            "release_commit",
            "source_commit",
            "sync_validation_run_attempt",
            "sync_validation_run_id",
        )
        if key in values
    }
    if any(safe.fullmatch(value) is None for value in selected.values()):
        raise ApprovalError("Refusing to write unsafe workflow output")
    try:
        with Path(path).open("a", encoding="utf-8", newline="\n") as stream:
            for key, value in selected.items():
                stream.write(f"{key}={value}\n")
    except OSError as exc:
        raise ApprovalError("Could not append workflow outputs") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    ready_parser = subparsers.add_parser("ready")
    recovery_parser = subparsers.add_parser("recover-ready")
    authorize_parser = subparsers.add_parser("authorize")
    verify_parser = subparsers.add_parser("verify-artifact")
    for subparser in (ready_parser, recovery_parser, authorize_parser):
        subparser.add_argument("--owner", required=True)
        subparser.add_argument("--repository", required=True)
        subparser.add_argument("--api-url", required=True)
        subparser.add_argument("--server-url", required=True)
        subparser.add_argument("--output", type=Path, required=True)
    ready_parser.add_argument("--version", required=True)
    ready_parser.add_argument("--source-commit", required=True)
    ready_parser.add_argument("--release-commit", required=True)
    ready_parser.add_argument("--pull-request", type=int, required=True)
    ready_parser.add_argument("--feature-readiness", type=Path, required=True)
    ready_parser.add_argument("--main-validation-run-id", type=int, required=True)
    ready_parser.add_argument(
        "--main-validation-run-attempt", type=int, required=True
    )
    ready_parser.add_argument("--manifest-sha256", required=True)
    ready_parser.add_argument("--preflight-run-id", type=int, required=True)
    ready_parser.add_argument("--preflight-run-attempt", type=int, required=True)
    ready_parser.add_argument("--preflight-artifact", required=True)
    recovery_parser.add_argument("--recovery-contract", type=Path, required=True)
    recovery_parser.add_argument("--feature-readiness", type=Path, required=True)
    recovery_parser.add_argument(
        "--validation-attestation", type=Path, required=True
    )
    recovery_parser.add_argument("--validation-artifact-id", type=int, required=True)
    recovery_parser.add_argument("--validation-artifact-name", required=True)
    recovery_parser.add_argument("--validation-artifact-digest", required=True)
    recovery_parser.add_argument("--work-actor", default="")
    authorize_parser.add_argument("--actor", required=True)
    authorize_parser.add_argument("--triggering-actor", required=True)
    authorize_parser.add_argument("--run-attempt", type=int, required=True)
    authorize_parser.add_argument("--event", type=Path, required=True)
    authorize_parser.add_argument("--work-actor", default="")
    verify_parser.add_argument("--report", type=Path, required=True)
    verify_parser.add_argument("--artifact-dir", type=Path, required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] = os.environ,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify-artifact":
            report = json.loads(args.report.read_text(encoding="utf-8"))
            verify_artifact(report, args.artifact_dir)
            stdout.write('{"result":"preflight-evidence-verified","schema_version":2}\n')
            return 0
        token = environment.get("GITHUB_TOKEN", "")
        if args.command == "ready":
            config = _config(
                owner=args.owner,
                repository=args.repository,
                version=args.version,
                source_commit=args.source_commit,
                release_commit=args.release_commit,
                api_url=args.api_url,
                server_url=args.server_url,
                output=args.output,
            )
            feature_readiness_raw = args.feature_readiness.read_text(encoding="ascii")
            feature_readiness = json.loads(feature_readiness_raw)
            if feature_readiness_raw != _canonical(feature_readiness) + "\n":
                raise ApprovalError("Feature-readiness evidence is not canonical")
            report = ready(
                config,
                token,
                number=args.pull_request,
                feature_readiness=feature_readiness,
                main_validation_run_id=args.main_validation_run_id,
                main_validation_run_attempt=args.main_validation_run_attempt,
                manifest_sha256=args.manifest_sha256,
                preflight_run_id=args.preflight_run_id,
                preflight_run_attempt=args.preflight_run_attempt,
                preflight_artifact=args.preflight_artifact,
            )
            output_values = report["ready"]
        elif args.command == "recover-ready":
            contract = validation_recovery.load_contract(args.recovery_contract)
            feature_readiness_raw = args.feature_readiness.read_text(encoding="ascii")
            feature_readiness = json.loads(feature_readiness_raw)
            if feature_readiness_raw != _canonical(feature_readiness) + "\n":
                raise ApprovalError("Feature-readiness evidence is not canonical")
            attestation_raw = args.validation_attestation.read_text(encoding="ascii")
            validation_attestation = json.loads(attestation_raw)
            if attestation_raw != _canonical(validation_attestation) + "\n":
                raise ApprovalError("Recovery validation evidence is not canonical")
            report = ready_recovery(
                contract,
                token,
                owner=args.owner,
                repository=args.repository,
                api_url=args.api_url,
                server_url=args.server_url,
                output=args.output,
                feature_readiness=feature_readiness,
                validation_attestation=validation_attestation,
                validation_artifact_id=args.validation_artifact_id,
                validation_artifact_name=args.validation_artifact_name,
                validation_artifact_digest=args.validation_artifact_digest,
                work_actor=args.work_actor,
            )
            output_values = report["ready"]
        else:
            event = json.loads(args.event.read_text(encoding="utf-8"))
            report = authorize(
                event,
                owner=args.owner,
                repository=args.repository,
                actor=args.actor,
                triggering_actor=args.triggering_actor,
                run_attempt=args.run_attempt,
                api_url=args.api_url,
                server_url=args.server_url,
                output=args.output,
                token=token,
                work_actor=args.work_actor,
            )
            output_values = report
        _write_json(args.output, report)
        _append_outputs(environment.get("GITHUB_OUTPUT"), output_values)
        stdout.write(_canonical(report) + "\n")
        return 0
    except (
        SyncPrError,
        validation_recovery.ValidationRecoveryError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        stderr.write(f"platform approval error: {str(exc).splitlines()[0][:500]}\n")
        return 2
    except Exception:
        stderr.write("platform approval error: unexpected internal failure\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
