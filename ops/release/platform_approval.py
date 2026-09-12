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
import http.client
import json
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from open_sync_pr import (
    GET_ATTEMPTS,
    HTTP_TIMEOUT_SECONDS,
    GitHubClient,
    SyncConfig,
    SyncPrError,
)
import validation_recovery


SCHEMA_VERSION = 2
RECOVERY_SCHEMA_VERSION = 7
CURRENT_RECOVERY_SCHEMA_VERSION = 8
FEATURE_READINESS_SCHEMA_VERSION = 1
PREFLIGHT_EVIDENCE_SCHEMA_VERSION = 1
READY_PREFIX = "<!-- buh-platform-ready:v2 "
APPROVAL_PREFIX = "<!-- buh-chatgpt-approved:v2 "
RECOVERY_READY_PREFIX = "<!-- buh-platform-ready-recovery:v1 "
RECOVERY_APPROVAL_PREFIX = "<!-- buh-chatgpt-approved-recovery:v1 "
CURRENT_RECOVERY_READY_PREFIX = "<!-- buh-platform-ready-recovery:v2 "
CURRENT_RECOVERY_APPROVAL_PREFIX = "<!-- buh-chatgpt-approved-recovery:v2 "
MARKER_SUFFIX = " -->"
BOT_LOGIN = "github-actions[bot]"
MAX_PAGES = 100
SOURCE_CI_POLL_SECONDS = 15
SOURCE_CI_POLLS = 160
RECOVERY_CHECK_BINDING_SCHEMA_VERSION = 3
RECOVERY_CHECK_POLLS = 20
RECOVERY_CHECK_POLL_SECONDS = 1
RECOVERY_SOURCE_JOB_PREFIX = "Validate unchanged v0.6.2 with the reviewed harness"
RECOVERY_WORKFLOW_NAME = "Validate Published Release Recovery"
RECOVERY_REQUIRED_CHECK_QUERY = """
query RequiredRecoveryCheck(
  $checkRunId: ID!
  $owner: String!
  $repository: String!
  $pullRequest: Int!
) {
  node(id: $checkRunId) {
    __typename
    ... on CheckRun {
      databaseId
      isRequired(pullRequestNumber: $pullRequest)
      name
      status
      conclusion
      detailsUrl
      externalId
      checkSuite {
        databaseId
        commit { oid }
      }
      repository { nameWithOwner }
    }
  }
  repository(owner: $owner, name: $repository) {
    nameWithOwner
    pullRequest(number: $pullRequest) {
      number
      state
      headRefOid
    }
  }
}
""".strip()
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
    expected_head: str | None = None,
    expected_base: str | None = None,
    require_mergeable: bool = False,
) -> dict[str, Any]:
    if not isinstance(pr, dict):
        raise ApprovalError("GitHub returned a malformed synchronization PR")
    head = pr.get("head")
    base = pr.get("base")
    expected_url = f"{config.server_url}/{config.repository}/pull/{number}"
    expected_head_commit = config.release_commit if expected_head is None else expected_head
    if (
        pr.get("number") != number
        or pr.get("state") != state
        or pr.get("draft") is not False
        or pr.get("title") != f"Sync platform release v{config.version}"
        or pr.get("html_url") != expected_url
        or _login(pr.get("user")) != config.owner
        or not isinstance(head, dict)
        or head.get("ref") != config.sync_branch
        or head.get("sha") != expected_head_commit
        or _repo_name(head.get("repo")) != config.repository
        or not isinstance(base, dict)
        or base.get("ref") != "main"
        or _repo_name(base.get("repo")) != config.repository
        or expected_base is not None
        and base.get("sha") != expected_base
        or require_mergeable
        and (pr.get("mergeable") is not True or pr.get("mergeable_state") != "clean")
    ):
        raise ApprovalError("Synchronization PR identity does not match the release")
    return pr


def _get_pr(
    client: GitHubClient,
    config: SyncConfig,
    number: int,
    state: str,
    *,
    expected_head: str | None = None,
    expected_base: str | None = None,
    require_mergeable: bool = False,
):
    return _validate_pr(
        client.get(_path(config.repository, f"pulls/{number}")),
        config,
        number,
        state=state,
        expected_head=expected_head,
        expected_base=expected_base,
        require_mergeable=require_mergeable,
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
    head_commit: str | None = None,
) -> dict[str, Any]:
    expected_head = config.release_commit if head_commit is None else head_commit
    for poll in range(polls):
        payload = client.get(
            _path(config.repository, "actions/workflows/source-ci.yml/runs"),
            {
                "event": "pull_request",
                "head_sha": expected_head,
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
            and run.get("head_sha") == expected_head
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


def _verify_review_merge_remote(
    client: GitHubClient,
    config: SyncConfig,
    identity: Mapping[str, Any],
    *,
    commit_field: str,
    tree_field: str,
    first_parent: str,
    label: str,
    state: str,
    work_actor: str,
) -> dict[str, Any]:
    merge_commit = identity[commit_field]
    feature_head = identity["feature_head"]
    merge_tree = identity[tree_field]
    pull_request = identity["pull_request"]
    commit = _read_commit(client, config, merge_commit)
    parents = commit.get("parents")
    if (
        not isinstance(parents, list)
        or len(parents) != 2
        or not all(isinstance(parent, dict) for parent in parents)
        or parents[0].get("sha") != first_parent
        or parents[1].get("sha") != feature_head
        or _commit_tree(commit, context=f"Recovery {label}") != merge_tree
    ):
        raise ApprovalError(f"Recovery {label} merge identity is invalid")
    feature = _read_commit(client, config, feature_head)
    if _commit_tree(feature, context=f"Recovery {label} feature") != merge_tree:
        raise ApprovalError(f"Recovery {label} tree differs from its feature head")
    pulls = _pages(
        client,
        _path(config.repository, f"commits/{merge_commit}/pulls"),
    )
    matches = [
        pull
        for pull in pulls
        if isinstance(pull, dict)
        and pull.get("number") == pull_request
    ]
    if len(matches) != 1:
        raise ApprovalError(f"Recovery {label} lacks one exact pull request")
    # GitHub's commit-to-PR association response is intentionally abbreviated
    # and does not include `merged_by`.  Use it only to bind the commit to one
    # exact PR, then obtain the complete PR before evaluating any identity.
    pull = client.get(
        _path(config.repository, f"pulls/{pull_request}")
    )
    if not isinstance(pull, dict):
        raise ApprovalError(f"Recovery {label} pull-request detail is malformed")
    head = pull.get("head")
    base = pull.get("base")
    if (
        pull.get("state") != state
        or pull.get("draft") is not False
        or pull.get("merge_commit_sha") != merge_commit
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
        raise ApprovalError(f"Recovery {label} pull-request identity is invalid")
    _timestamp(f"Recovery {label} merge timestamp", pull.get("merged_at"))
    return pull


def _verify_activation_remote(
    client: GitHubClient,
    config: SyncConfig,
    attestation: Mapping[str, Any],
    *,
    state: str,
    work_actor: str,
) -> dict[str, Any]:
    return _verify_review_merge_remote(
        client,
        config,
        attestation["activation"],
        commit_field="activation_commit",
        tree_field="activation_tree",
        first_parent=config.source_sha,
        label="activation",
        state=state,
        work_actor=work_actor,
    )


def _verify_continuation_remote(
    client: GitHubClient,
    config: SyncConfig,
    attestation: Mapping[str, Any],
    *,
    state: str,
    work_actor: str,
) -> dict[str, Any]:
    return _verify_review_merge_remote(
        client,
        config,
        attestation["continuation"],
        commit_field="continuation_commit",
        tree_field="continuation_tree",
        first_parent=attestation["activation"]["activation_commit"],
        label="continuation",
        state=state,
        work_actor=work_actor,
    )


def _verify_repair_remote(
    client: GitHubClient,
    config: SyncConfig,
    attestation: Mapping[str, Any],
    *,
    state: str,
    work_actor: str,
) -> dict[str, Any]:
    return _verify_review_merge_remote(
        client,
        config,
        attestation["repair"],
        commit_field="repair_commit",
        tree_field="repair_tree",
        first_parent=attestation["continuation"]["continuation_commit"],
        label="digest repair",
        state=state,
        work_actor=work_actor,
    )


def _verify_publication_repair_remote(
    client: GitHubClient,
    config: SyncConfig,
    identity: Mapping[str, Any],
    *,
    state: str,
    work_actor: str,
) -> dict[str, Any]:
    return _verify_review_merge_remote(
        client,
        config,
        identity,
        commit_field="publication_repair_commit",
        tree_field="publication_repair_tree",
        first_parent=identity["base_commit"],
        label="publication repair",
        state=state,
        work_actor=work_actor,
    )


def _publication_repair_evidence(
    value: Any,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "base_commit",
        "feature_head",
        "paths",
        "publication_repair_commit",
        "publication_repair_tree",
        "pull_request",
    }:
        raise ApprovalError("Recovery publication repair evidence is invalid")
    publication = contract["publication_repair"]
    commit = value.get("publication_repair_commit")
    feature_head = value.get("feature_head")
    tree = value.get("publication_repair_tree")
    paths = value.get("paths")
    if (
        value.get("base_commit") != publication["base_commit"]
        or value.get("pull_request") != publication["pull_request"]
        or not isinstance(commit, str)
        or COMMIT_RE.fullmatch(commit) is None
        or not isinstance(feature_head, str)
        or COMMIT_RE.fullmatch(feature_head) is None
        or not isinstance(tree, str)
        or COMMIT_RE.fullmatch(tree) is None
        or not isinstance(paths, list)
        or [item.get("path") for item in paths if isinstance(item, dict)]
        != list(validation_recovery.PUBLICATION_REPAIR_PATHS)
    ):
        raise ApprovalError("Recovery publication repair identity changed")
    for item in paths:
        if (
            not isinstance(item, dict)
            or set(item) != {"git_blob_sha", "path", "sha256"}
            or not isinstance(item.get("git_blob_sha"), str)
            or COMMIT_RE.fullmatch(item["git_blob_sha"]) is None
            or not isinstance(item.get("sha256"), str)
            or NONCE_RE.fullmatch(item["sha256"]) is None
        ):
            raise ApprovalError("Recovery publication repair path evidence changed")
    return {
        "base_commit": publication["base_commit"],
        "feature_head": feature_head,
        "paths": [dict(item) for item in paths],
        "publication_repair_commit": commit,
        "publication_repair_tree": tree,
        "pull_request": publication["pull_request"],
    }


def _sync_repair_evidence(
    value: Any,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "base_commit",
        "feature_head",
        "paths",
        "pull_request",
        "sync_repair_commit",
        "sync_repair_tree",
    }:
        raise ApprovalError("Synchronization-head repair evidence is invalid")
    repair = contract["sync_repair"]
    paths = value.get("paths")
    if (
        value.get("base_commit") != repair["base_commit"]
        or value.get("pull_request") != repair["pull_request"]
        or not isinstance(value.get("feature_head"), str)
        or COMMIT_RE.fullmatch(value["feature_head"]) is None
        or not isinstance(value.get("sync_repair_commit"), str)
        or COMMIT_RE.fullmatch(value["sync_repair_commit"]) is None
        or not isinstance(value.get("sync_repair_tree"), str)
        or COMMIT_RE.fullmatch(value["sync_repair_tree"]) is None
        or not isinstance(paths, list)
        or [item.get("path") for item in paths if isinstance(item, dict)]
        != list(validation_recovery.SYNC_REPAIR_PATHS)
    ):
        raise ApprovalError("Synchronization-head repair identity changed")
    for item in paths:
        if (
            not isinstance(item, dict)
            or set(item) != {"git_blob_sha", "path", "sha256"}
            or not isinstance(item.get("git_blob_sha"), str)
            or COMMIT_RE.fullmatch(item["git_blob_sha"]) is None
            or not isinstance(item.get("sha256"), str)
            or NONCE_RE.fullmatch(item["sha256"]) is None
        ):
            raise ApprovalError("Synchronization-head repair path evidence changed")
    return {
        "base_commit": repair["base_commit"],
        "feature_head": value["feature_head"],
        "paths": [dict(item) for item in paths],
        "pull_request": repair["pull_request"],
        "sync_repair_commit": value["sync_repair_commit"],
        "sync_repair_tree": value["sync_repair_tree"],
    }


def _sync_update_evidence(
    value: Any,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "base_commit",
        "initial_head_commit",
        "pull_request",
        "release_commit",
        "sync_branch",
        "sync_head_commit",
        "sync_head_tree",
        "sync_repair",
    }:
        raise ApprovalError("Synchronization update evidence is invalid")
    repair = _sync_repair_evidence(value.get("sync_repair"), contract)
    sync = contract["sync"]
    release = contract["release"]
    if (
        value.get("base_commit") != repair["sync_repair_commit"]
        or value.get("initial_head_commit") != sync["initial_head_commit"]
        or value.get("pull_request") != sync["pull_request"]
        or value.get("release_commit") != release["commit"]
        or value.get("sync_branch") != sync["branch"]
        or not isinstance(value.get("sync_head_commit"), str)
        or COMMIT_RE.fullmatch(value["sync_head_commit"]) is None
        or not isinstance(value.get("sync_head_tree"), str)
        or COMMIT_RE.fullmatch(value["sync_head_tree"]) is None
    ):
        raise ApprovalError("Synchronization update identity changed")
    return {**value, "sync_repair": repair}


def _verify_sync_repair_remote(
    client: GitHubClient,
    config: SyncConfig,
    identity: Mapping[str, Any],
    *,
    work_actor: str,
) -> dict[str, Any]:
    return _verify_review_merge_remote(
        client,
        config,
        identity,
        commit_field="sync_repair_commit",
        tree_field="sync_repair_tree",
        first_parent=identity["base_commit"],
        label="synchronization-head repair",
        state="closed",
        work_actor=work_actor,
    )


def _verify_sync_update_remote(
    client: GitHubClient,
    config: SyncConfig,
    contract: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> None:
    sync_head = identity["sync_head_commit"]
    commit = _read_commit(client, config, sync_head)
    parents = commit.get("parents")
    if (
        not isinstance(parents, list)
        or len(parents) != 2
        or not all(isinstance(parent, dict) for parent in parents)
        or parents[0].get("sha") != config.release_commit
        or parents[1].get("sha") != identity["base_commit"]
        or _commit_tree(commit, context="Synchronization update")
        != identity["sync_head_tree"]
    ):
        raise ApprovalError("Synchronization update commit identity is invalid")
    if _read_ref(client, config, config.sync_branch) != sync_head:
        raise ApprovalError("Synchronization branch moved after validation")


def _required_check_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    required = contract.get("required_check")
    if not isinstance(required, dict) or set(required) != {
        "app_id",
        "app_slug",
        "branch",
        "context",
        "historical_failure",
        "required_lanes",
    }:
        raise ApprovalError("Recovery required-check contract is invalid")
    historical = required.get("historical_failure")
    if not isinstance(historical, dict) or set(historical) != {
        "check_run_id",
        "conclusion",
        "details_url",
        "run_attempt",
        "workflow_run_id",
    }:
        raise ApprovalError("Recovery historical check contract is invalid")
    if (
        required.get("app_id") != validation_recovery.REQUIRED_CHECK_APP_ID
        or required.get("app_slug") != validation_recovery.REQUIRED_CHECK_APP_SLUG
        or required.get("branch") != "main"
        or required.get("context") != validation_recovery.REQUIRED_CHECK_CONTEXT
        or required.get("required_lanes")
        != list(validation_recovery.RECOVERY_REQUIRED_LANES)
        or historical.get("conclusion") != "failure"
    ):
        raise ApprovalError("Recovery required-check contract changed")
    _safe_int("Historical required check ID", historical.get("check_run_id"))
    _safe_int("Historical required workflow ID", historical.get("workflow_run_id"))
    if _safe_int(
        "Historical required workflow attempt",
        historical.get("run_attempt"),
        maximum=10**6,
    ) != 1:
        raise ApprovalError("Recovery historical check rerun is forbidden")
    details_url = historical.get("details_url")
    if not isinstance(details_url, str) or len(details_url) > 500:
        raise ApprovalError("Recovery historical check URL is invalid")
    return required


def _required_check_app(value: Any) -> tuple[int | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    app_id = value.get("id")
    return (app_id if type(app_id) is int else None, value.get("slug"))


def _required_check_pull_matches(
    value: Any,
    *,
    number: int,
    release_commit: str,
) -> bool:
    if not isinstance(value, list) or len(value) != 1:
        return False
    pull = value[0]
    if not isinstance(pull, dict):
        return False
    head = pull.get("head")
    base = pull.get("base")
    return (
        pull.get("number") == number
        and isinstance(head, dict)
        and head.get("sha") == release_commit
        and isinstance(base, dict)
        and base.get("ref") == "main"
    )


def _canonical_check_url(config: SyncConfig, check_run_id: int) -> str:
    """Return GitHub's canonical URL for a repository check-run resource."""

    checked_id = _safe_int("Required release validation check ID", check_run_id)
    return f"{config.server_url}/{config.repository}/runs/{checked_id}"


def _validate_required_check_run(
    value: Any,
    config: SyncConfig,
    contract: Mapping[str, Any],
    *,
    check_run_id: int,
    conclusion: str,
    details_url: str,
    external_id: str | None = None,
    output: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    required = _required_check_contract(contract)
    if not isinstance(value, dict):
        raise ApprovalError("Required release validation check is malformed")
    app_id, app_slug = _required_check_app(value.get("app"))
    if (
        value.get("id") != check_run_id
        or value.get("name") != required["context"]
        or value.get("head_sha") != config.release_commit
        or value.get("status") != "completed"
        or value.get("conclusion") != conclusion
        or value.get("details_url") != details_url
        or app_id != required["app_id"]
        or app_slug != required["app_slug"]
    ):
        raise ApprovalError("Required release validation check identity is invalid")
    associations = value.get("pull_requests")
    # GitHub may clear check/run PR arrays after merge. At readiness, the live
    # open PR is verified separately; at authorization, the merged event and
    # two-parent merge are verified. Never accept a conflicting association.
    if associations != [] and not _required_check_pull_matches(
        associations,
        number=contract["sync"]["pull_request"],
        release_commit=config.release_commit,
    ):
        raise ApprovalError("Required release validation check association is invalid")
    if external_id is not None and value.get("external_id") != external_id:
        raise ApprovalError("Required release validation check binding is invalid")
    if output is not None:
        actual_output = value.get("output")
        if not isinstance(actual_output, dict) or any(
            actual_output.get(key) != expected for key, expected in output.items()
        ):
            raise ApprovalError("Required release validation check report is invalid")
    return value


def _verify_required_check_protection(
    client: GitHubClient,
    config: SyncConfig,
    contract: Mapping[str, Any],
) -> None:
    required = _required_check_contract(contract)
    branch = client.get(
        _path(config.repository, f"branches/{required['branch']}")
    )
    protection = branch.get("protection") if isinstance(branch, dict) else None
    status = (
        protection.get("required_status_checks")
        if isinstance(protection, dict)
        else None
    )
    expected_check = {
        "app_id": required["app_id"],
        "context": required["context"],
    }
    checks = status.get("checks") if isinstance(status, dict) else None
    contexts = status.get("contexts") if isinstance(status, dict) else None
    if (
        not isinstance(branch, dict)
        or branch.get("name") != required["branch"]
        or branch.get("protected") is not True
        or not isinstance(status, dict)
        or status.get("enforcement_level") != "everyone"
        or not isinstance(checks, list)
        or not all(
            isinstance(item, dict)
            and type(item.get("app_id")) is int
            and isinstance(item.get("context"), str)
            and bool(item["context"])
            for item in checks
        )
        or checks.count(expected_check) != 1
        or not isinstance(contexts, list)
        or not all(isinstance(item, str) and bool(item) for item in contexts)
        or contexts.count(required["context"]) != 1
    ):
        raise ApprovalError("Required release validation branch protection changed")


def _verify_historical_required_check(
    client: GitHubClient,
    config: SyncConfig,
    contract: Mapping[str, Any],
    *,
    work_actor: str,
) -> dict[str, Any]:
    required = _required_check_contract(contract)
    historical = required["historical_failure"]
    check = client.get(
        _path(config.repository, f"check-runs/{historical['check_run_id']}")
    )
    checked = _validate_required_check_run(
        check,
        config,
        contract,
        check_run_id=historical["check_run_id"],
        conclusion="failure",
        details_url=historical["details_url"],
    )
    run = client.get(
        _path(config.repository, f"actions/runs/{historical['workflow_run_id']}")
    )
    associations = run.get("pull_requests") if isinstance(run, dict) else None
    if (
        not isinstance(run, dict)
        or run.get("id") != historical["workflow_run_id"]
        or run.get("run_attempt") != historical["run_attempt"]
        or run.get("status") != "completed"
        or run.get("conclusion") != "failure"
        or run.get("event") != "pull_request"
        or run.get("head_sha") != config.release_commit
        or run.get("head_branch") != config.sync_branch
        or _workflow_path(run) != ".github/workflows/source-ci.yml"
        or _repo_name(run.get("repository")) != config.repository
        or _repo_name(run.get("head_repository")) != config.repository
        or (
            associations != []
            and _run_pr_numbers(run) != {contract["sync"]["pull_request"]}
        )
        or not _allowed_operator(_login(run.get("actor")), config, work_actor)
        or not _allowed_operator(
            _login(run.get("triggering_actor")), config, work_actor
        )
    ):
        raise ApprovalError("Historical required validation failure changed")
    return checked


def _recovery_lane_jobs(
    client: GitHubClient,
    config: SyncConfig,
    contract: Mapping[str, Any],
    *,
    run_id: int,
    run_attempt: int,
    activation_commit: str,
) -> list[dict[str, Any]]:
    required = _required_check_contract(contract)
    payload = client.get(
        _path(config.repository, f"actions/runs/{run_id}/jobs"),
        {"filter": "latest", "per_page": "100"},
    )
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    total = payload.get("total_count") if isinstance(payload, dict) else None
    if (
        not isinstance(jobs, list)
        or type(total) is not int
        or total != len(jobs)
        or total > 100
    ):
        raise ApprovalError("Recovery validation job list is malformed")
    normalized = []
    for lane in required["required_lanes"]:
        expected_name = f"{RECOVERY_SOURCE_JOB_PREFIX} / {lane}"
        matches = [
            job
            for job in jobs
            if isinstance(job, dict) and job.get("name") == expected_name
        ]
        if len(matches) != 1:
            raise ApprovalError("Recovery validation required lane is unavailable")
        job = matches[0]
        if (
            job.get("status") != "completed"
            or job.get("conclusion") != "success"
            or job.get("run_id") != run_id
            or job.get("run_attempt") != run_attempt
            or job.get("head_sha") != activation_commit
        ):
            raise ApprovalError("Recovery validation required lane did not pass")
        normalized.append(
            {
                "conclusion": "success",
                "id": _safe_int("Recovery validation job ID", job.get("id")),
                "name": expected_name,
            }
        )
    return normalized


def _recovery_check_binding(
    contract: Mapping[str, Any],
    attestation: Mapping[str, Any],
    artifact: Mapping[str, Any],
    lane_jobs: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    validation = attestation["validation"]
    binding = {
        "activation": attestation["activation"],
        "artifact": {
            "digest": artifact["digest"],
            "id": artifact["id"],
            "name": artifact["name"],
        },
        "attestation_sha256": "sha256:"
        + hashlib.sha256(
            validation_recovery.canonical_json_bytes(attestation)
        ).hexdigest(),
        "continuation": attestation["continuation"],
        "harness": attestation["harness"],
        "lanes": list(lane_jobs),
        "repair": attestation["repair"],
        "recovery_id": contract["recovery_id"],
        "release": contract["release"],
        "repository": contract["repository"],
        "schema_version": RECOVERY_CHECK_BINDING_SCHEMA_VERSION,
        "workflow": {
            "path": validation_recovery.WORKFLOW_PATH,
            "run_attempt": validation["run_attempt"],
            "run_id": validation["run_id"],
        },
    }
    digest = "sha256:" + hashlib.sha256(_canonical(binding).encode("ascii")).hexdigest()
    return binding, digest


def _recovery_check_payload(
    config: SyncConfig,
    contract: Mapping[str, Any],
    attestation: Mapping[str, Any],
    artifact: Mapping[str, Any],
    lane_jobs: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str, str]:
    required = _required_check_contract(contract)
    binding, binding_digest = _recovery_check_binding(
        contract, attestation, artifact, lane_jobs
    )
    run_id = attestation["validation"]["run_id"]
    details_url = f"{config.server_url}/{config.repository}/actions/runs/{run_id}"
    external_id = f"{contract['recovery_id']}:{binding_digest.removeprefix('sha256:')}"
    lane_lines = "\n".join(
        f"- `{item['name']}` (job `{item['id']}`): success" for item in lane_jobs
    )
    summary = (
        "The complete required source suite passed against immutable release "
        f"`{config.release_commit}`. Only the two attested test-fixture files "
        f"from activation/harness `{attestation['harness']['commit']}` were overlaid; "
        "release and runtime bytes were not rebuilt or changed.\n\n"
        f"Recovery run/attempt: `{run_id}/"
        f"{attestation['validation']['run_attempt']}`  \n"
        f"Release tree: `{contract['release']['tree']}`  \n"
        f"Attestation artifact: `{artifact['name']}` / `{artifact['id']}` / "
        f"`{artifact['digest']}`  \n"
        f"Attestation SHA-256: `{binding['attestation_sha256']}`  \n"
        f"Evidence binding: `{binding_digest}`\n\n"
        f"Required lanes:\n{lane_lines}"
    )
    payload = {
        "conclusion": "success",
        "details_url": details_url,
        "external_id": external_id,
        "head_sha": config.release_commit,
        "name": required["context"],
        "output": {
            "summary": summary,
            "title": "Validated immutable platform v0.6.2 with reviewed recovery harness",
        },
        "status": "completed",
    }
    return payload, binding_digest, external_id


def _check_runs_for_release(
    client: GitHubClient,
    config: SyncConfig,
    contract: Mapping[str, Any],
    *,
    filter_value: str,
) -> list[dict[str, Any]]:
    required = _required_check_contract(contract)
    payload = client.get(
        _path(config.repository, f"commits/{config.release_commit}/check-runs"),
        {
            "app_id": str(required["app_id"]),
            "check_name": required["context"],
            "filter": filter_value,
            "per_page": "100",
        },
    )
    runs = payload.get("check_runs") if isinstance(payload, dict) else None
    total = payload.get("total_count") if isinstance(payload, dict) else None
    if (
        not isinstance(runs, list)
        or type(total) is not int
        or total != len(runs)
        or total > 100
        or any(not isinstance(item, dict) for item in runs)
    ):
        raise ApprovalError("Required release validation check list is malformed")
    return runs


def _graphql_read(
    client: GitHubClient,
    query: str,
    variables: Mapping[str, Any],
) -> dict[str, Any]:
    body = (_canonical({"query": query, "variables": variables}) + "\n").encode(
        "utf-8"
    )
    for attempt in range(GET_ATTEMPTS):
        try:
            response = client.transport.request(
                "POST",
                f"{client.api_url.rstrip('/')}/graphql",
                {**client.headers, "Content-Type": "application/json"},
                body,
                HTTP_TIMEOUT_SECONDS,
            )
        except (
            OSError,
            TimeoutError,
            http.client.HTTPException,
            urllib.error.URLError,
        ):
            if attempt + 1 == GET_ATTEMPTS:
                raise SyncPrError(
                    "GitHub GraphQL read failed after bounded retries"
                ) from None
        else:
            if response.status == 200:
                try:
                    payload = json.loads(response.body.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    raise ApprovalError(
                        "GitHub returned invalid required-check GraphQL data"
                    ) from None
                if not isinstance(payload, dict) or payload.get("errors") is not None:
                    raise ApprovalError(
                        "GitHub returned invalid required-check GraphQL data"
                    )
                return payload
            retryable = response.status in {408, 425, 429} or (
                500 <= response.status <= 599
            )
            if not retryable or attempt + 1 == GET_ATTEMPTS:
                raise SyncPrError(
                    f"GitHub GraphQL read failed with HTTP {response.status}"
                )
        client.sleeper(0.25 * (2**attempt))
    raise SyncPrError("GitHub GraphQL read exhausted its fixed attempt limit")


def _opaque_check_node_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 4 <= len(value) <= 256
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ApprovalError("Required release validation check node ID is invalid")
    return value


def _required_check_locator(value: Mapping[str, Any]) -> dict[str, Any]:
    suite = value.get("check_suite")
    if not isinstance(suite, dict):
        raise ApprovalError("Required release validation check suite is invalid")
    return {
        "check_run_id": _safe_int(
            "Required release validation check ID", value.get("id")
        ),
        "check_run_node_id": _opaque_check_node_id(value.get("node_id")),
        "check_suite_id": _safe_int(
            "Required release validation check suite ID", suite.get("id")
        ),
        "completed_at": _timestamp(
            "Required release validation check completion", value.get("completed_at")
        ),
    }


def _listed_required_check(
    value: Any,
    config: SyncConfig,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    required = _required_check_contract(contract)
    if not isinstance(value, dict):
        raise ApprovalError("Required release validation check list is malformed")
    app_id, app_slug = _required_check_app(value.get("app"))
    associations = value.get("pull_requests")
    status = value.get("status")
    conclusion = value.get("conclusion")
    completed_at = value.get("completed_at")
    if (
        value.get("name") != required["context"]
        or value.get("head_sha") != config.release_commit
        or app_id != required["app_id"]
        or app_slug != required["app_slug"]
        or (
            associations != []
            and not _required_check_pull_matches(
                associations,
                number=contract["sync"]["pull_request"],
                release_commit=config.release_commit,
            )
        )
        or status
        not in {
            "queued",
            "in_progress",
            "completed",
            "waiting",
            "requested",
            "pending",
        }
    ):
        raise ApprovalError("Required release validation check list is malformed")
    if status == "completed":
        if conclusion not in {
            "action_required",
            "cancelled",
            "failure",
            "neutral",
            "success",
            "skipped",
            "stale",
            "timed_out",
        }:
            raise ApprovalError("Required release validation check list is malformed")
        completion = _timestamp(
            "Required release validation check completion", completed_at
        )
    else:
        if conclusion is not None or completed_at is not None:
            raise ApprovalError("Required release validation check list is malformed")
        completion = None
    external_id = value.get("external_id")
    if external_id is not None and (
        not isinstance(external_id, str)
        or len(external_id) > 1000
        or any(ord(character) < 32 for character in external_id)
    ):
        raise ApprovalError("Required release validation check list is malformed")
    suite = value.get("check_suite")
    if not isinstance(suite, dict):
        raise ApprovalError("Required release validation check list is malformed")
    return {
        "check_run_id": _safe_int(
            "Required release validation check ID", value.get("id")
        ),
        "check_run_node_id": _opaque_check_node_id(value.get("node_id")),
        "check_suite_id": _safe_int(
            "Required release validation check suite ID", suite.get("id")
        ),
        "completed_at": completion,
        "conclusion": conclusion,
        "external_id": external_id,
        "status": status,
        "value": value,
    }


def _index_required_checks(
    runs: Sequence[Mapping[str, Any]],
    config: SyncConfig,
    contract: Mapping[str, Any],
    *,
    latest: bool,
) -> dict[int, dict[str, Any]]:
    indexed: dict[int, dict[str, Any]] = {}
    suites: set[int] = set()
    for value in runs:
        normalized = _listed_required_check(value, config, contract)
        check_run_id = normalized["check_run_id"]
        check_suite_id = normalized["check_suite_id"]
        if check_run_id in indexed or (latest and check_suite_id in suites):
            raise ApprovalError("Required release validation check list is malformed")
        indexed[check_run_id] = normalized
        suites.add(check_suite_id)
    return indexed


def _same_required_check_locator(
    listed: Mapping[str, Any], locator: Mapping[str, Any]
) -> bool:
    return all(listed.get(key) == locator.get(key) for key in locator)


def _verify_exact_required_check_graphql(
    client: GitHubClient,
    config: SyncConfig,
    contract: Mapping[str, Any],
    check: Mapping[str, Any],
    *,
    pull_request_state: str,
    head_commit: str | None = None,
) -> None:
    owner, repository = config.repository.split("/", 1)
    locator = _required_check_locator(check)
    variables = {
        "checkRunId": locator["check_run_node_id"],
        "owner": owner,
        "repository": repository,
        "pullRequest": contract["sync"]["pull_request"],
    }
    expected_head = config.release_commit if head_commit is None else head_commit
    for attempt in range(RECOVERY_CHECK_POLLS):
        payload = _graphql_read(
            client,
            RECOVERY_REQUIRED_CHECK_QUERY,
            variables,
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ApprovalError("GitHub returned invalid required-check GraphQL data")
        node = data.get("node")
        remote_repository = data.get("repository")
        pull = (
            remote_repository.get("pullRequest")
            if isinstance(remote_repository, dict)
            else None
        )
        if (
            not isinstance(remote_repository, dict)
            or remote_repository.get("nameWithOwner") != config.repository
            or not isinstance(pull, dict)
            or pull.get("number") != contract["sync"]["pull_request"]
            or pull.get("state") != pull_request_state
            or pull.get("headRefOid") != expected_head
        ):
            raise ApprovalError(
                "Exact recovery check does not satisfy the protected pull-request requirement"
            )
        # GitHub can briefly return a complete PR object before the newly
        # created CheckRun node is visible through GraphQL. Only that precise
        # absence is transient; malformed, conflicting, or non-required nodes
        # below fail immediately.
        if node is None:
            if attempt + 1 < RECOVERY_CHECK_POLLS:
                client.sleeper(RECOVERY_CHECK_POLL_SECONDS)
                continue
            raise ApprovalError(
                "Exact recovery check did not become visible within the bounded poll"
            )
        suite = node.get("checkSuite") if isinstance(node, dict) else None
        commit = suite.get("commit") if isinstance(suite, dict) else None
        check_repository = node.get("repository") if isinstance(node, dict) else None
        if (
            not isinstance(node, dict)
            or node.get("__typename") != "CheckRun"
            or node.get("databaseId") != locator["check_run_id"]
            or node.get("isRequired") is not True
            or node.get("name") != check.get("name")
            or node.get("status") != str(check.get("status", "")).upper()
            or node.get("conclusion") != str(check.get("conclusion", "")).upper()
            or node.get("detailsUrl") != check.get("details_url")
            or node.get("externalId") != check.get("external_id")
            or not isinstance(suite, dict)
            or suite.get("databaseId") != locator["check_suite_id"]
            or not isinstance(commit, dict)
            or commit.get("oid") != expected_head
            or not isinstance(check_repository, dict)
            or check_repository.get("nameWithOwner") != config.repository
        ):
            raise ApprovalError(
                "Exact recovery check does not satisfy the protected pull-request requirement"
            )
        return


def _verify_historical_check_is_current(
    runs: Sequence[Mapping[str, Any]],
    config: SyncConfig,
    contract: Mapping[str, Any],
    historical: Mapping[str, Any],
) -> None:
    indexed = _index_required_checks(runs, config, contract, latest=True)
    locator = _required_check_locator(historical)
    listed = indexed.get(locator["check_run_id"])
    if listed is None or not _same_required_check_locator(listed, locator):
        raise ApprovalError("Historical failed check is not a current suite result")
    for check_run_id, candidate in indexed.items():
        if check_run_id == locator["check_run_id"]:
            continue
        if (
            candidate["status"] != "completed"
            or candidate["completed_at"] >= locator["completed_at"]
        ):
            raise ApprovalError(
                "Historical failed check has conflicting current-suite evidence"
            )


def _published_check_is_current(
    runs: Sequence[Mapping[str, Any]],
    config: SyncConfig,
    contract: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    indexed = _index_required_checks(runs, config, contract, latest=True)
    locator = _required_check_locator(current)
    listed = indexed.get(locator["check_run_id"])
    if listed is not None:
        _validate_required_check_run(
            listed["value"],
            config,
            contract,
            check_run_id=locator["check_run_id"],
            conclusion="success",
            details_url=current["details_url"],
            external_id=current["external_id"],
        )
        if not _same_required_check_locator(listed, locator):
            raise ApprovalError("Recovered required check list identity changed")
    for check_run_id, candidate in indexed.items():
        if check_run_id == locator["check_run_id"]:
            continue
        if candidate["check_suite_id"] == locator["check_suite_id"]:
            raise ApprovalError("Recovered required check was superseded in its suite")
        if candidate["external_id"] == current["external_id"]:
            raise ApprovalError("Recovered required check has conflicting evidence")
        if (
            candidate["status"] != "completed"
            or candidate["completed_at"] >= locator["completed_at"]
        ):
            raise ApprovalError(
                "Recovered required check has superseding or conflicting evidence"
            )
    return listed is not None


def _published_check_history_is_present(
    runs: Sequence[Mapping[str, Any]],
    config: SyncConfig,
    contract: Mapping[str, Any],
    current: Mapping[str, Any],
    historical: Mapping[str, Any],
) -> bool:
    indexed = _index_required_checks(runs, config, contract, latest=False)
    current_locator = _required_check_locator(current)
    historical_locator = _required_check_locator(historical)
    current_listed = indexed.get(current_locator["check_run_id"])
    historical_listed = indexed.get(historical_locator["check_run_id"])
    if current_listed is None or historical_listed is None:
        return False
    _validate_required_check_run(
        current_listed["value"],
        config,
        contract,
        check_run_id=current_locator["check_run_id"],
        conclusion="success",
        details_url=current["details_url"],
        external_id=current["external_id"],
    )
    _validate_required_check_run(
        historical_listed["value"],
        config,
        contract,
        check_run_id=historical_locator["check_run_id"],
        conclusion="failure",
        details_url=historical["details_url"],
    )
    if not _same_required_check_locator(
        current_listed, current_locator
    ) or not _same_required_check_locator(historical_listed, historical_locator):
        raise ApprovalError("Required check history identity changed during recovery")
    return True


def _verify_published_recovery_check(
    client: GitHubClient,
    config: SyncConfig,
    contract: Mapping[str, Any],
    attestation: Mapping[str, Any],
    artifact: Mapping[str, Any],
    lane_jobs: Sequence[Mapping[str, Any]],
    recorded: Mapping[str, Any],
    *,
    pull_request_state: str,
    work_actor: str,
    require_current_pr: bool = True,
) -> dict[str, Any]:
    required = _required_check_contract(contract)
    expected_payload, binding_digest, external_id = _recovery_check_payload(
        config, contract, attestation, artifact, lane_jobs
    )
    if not isinstance(recorded, dict) or set(recorded) != {
        "app_id",
        "app_slug",
        "binding_digest",
        "check_run_node_id",
        "check_run_id",
        "check_suite_id",
        "completed_at",
        "context",
        "details_url",
        "external_id",
        "head_sha",
        "historical_check_run_id",
        "lanes",
        "required_pull_request",
    }:
        raise ApprovalError("Recovery required-check evidence schema is invalid")
    check_run_id = _safe_int(
        "Recovered required check ID", recorded.get("check_run_id")
    )
    canonical_details_url = _canonical_check_url(config, check_run_id)
    check_suite_id = _safe_int(
        "Recovered required check suite ID", recorded.get("check_suite_id")
    )
    check_run_node_id = _opaque_check_node_id(recorded.get("check_run_node_id"))
    completed_at = _timestamp(
        "Recovered required check completion", recorded.get("completed_at")
    )
    if (
        recorded.get("app_id") != required["app_id"]
        or recorded.get("app_slug") != required["app_slug"]
        or recorded.get("binding_digest") != binding_digest
        or recorded.get("context") != required["context"]
        or recorded.get("details_url") != canonical_details_url
        or recorded.get("external_id") != external_id
        or recorded.get("head_sha") != config.release_commit
        or recorded.get("historical_check_run_id")
        != required["historical_failure"]["check_run_id"]
        or recorded.get("lanes") != list(lane_jobs)
        or recorded.get("required_pull_request")
        != contract["sync"]["pull_request"]
    ):
        raise ApprovalError("Recovery required-check evidence changed")
    _verify_required_check_protection(client, config, contract)
    historical = _verify_historical_required_check(
        client, config, contract, work_actor=work_actor
    )
    current = client.get(_path(config.repository, f"check-runs/{check_run_id}"))
    checked = _validate_required_check_run(
        current,
        config,
        contract,
        check_run_id=check_run_id,
        conclusion="success",
        details_url=canonical_details_url,
        external_id=external_id,
        output=expected_payload["output"],
    )
    if _required_check_locator(checked) != {
        "check_run_id": check_run_id,
        "check_run_node_id": check_run_node_id,
        "check_suite_id": check_suite_id,
        "completed_at": completed_at,
    }:
        raise ApprovalError("Recovery required-check locator changed")
    latest = []
    for attempt in range(RECOVERY_CHECK_POLLS):
        latest = _check_runs_for_release(
            client, config, contract, filter_value="latest"
        )
        if _published_check_is_current(latest, config, contract, checked):
            break
        if attempt + 1 < RECOVERY_CHECK_POLLS:
            client.sleeper(RECOVERY_CHECK_POLL_SECONDS)
    else:
        raise ApprovalError("Recovered required check is not a current suite result")
    if require_current_pr:
        _verify_exact_required_check_graphql(
            client,
            config,
            contract,
            checked,
            pull_request_state=pull_request_state,
        )
    all_runs = []
    for attempt in range(RECOVERY_CHECK_POLLS):
        all_runs = _check_runs_for_release(
            client, config, contract, filter_value="all"
        )
        if _published_check_history_is_present(
            all_runs, config, contract, checked, historical
        ):
            break
        if attempt + 1 < RECOVERY_CHECK_POLLS:
            client.sleeper(RECOVERY_CHECK_POLL_SECONDS)
    else:
        raise ApprovalError("Required check history is incomplete during recovery")
    return checked


def _publish_recovery_check(
    client: GitHubClient,
    config: SyncConfig,
    contract: Mapping[str, Any],
    attestation: Mapping[str, Any],
    artifact: Mapping[str, Any],
    lane_jobs: Sequence[Mapping[str, Any]],
    *,
    work_actor: str,
) -> dict[str, Any]:
    required = _required_check_contract(contract)
    _verify_required_check_protection(client, config, contract)
    historical = _verify_historical_required_check(
        client, config, contract, work_actor=work_actor
    )
    previous = _check_runs_for_release(
        client, config, contract, filter_value="latest"
    )
    _verify_historical_check_is_current(previous, config, contract, historical)
    _verify_exact_required_check_graphql(
        client,
        config,
        contract,
        historical,
        pull_request_state="OPEN",
    )
    payload, binding_digest, external_id = _recovery_check_payload(
        config, contract, attestation, artifact, lane_jobs
    )
    created = client.post(
        _path(config.repository, "check-runs"),
        payload,
        context="required release validation check publication",
    )
    check_run_id = _safe_int(
        "Recovered required check ID",
        created.get("id") if isinstance(created, dict) else None,
    )
    canonical_details_url = _canonical_check_url(config, check_run_id)
    _validate_required_check_run(
        created,
        config,
        contract,
        check_run_id=check_run_id,
        conclusion="success",
        details_url=canonical_details_url,
        external_id=external_id,
        output=payload["output"],
    )
    locator = _required_check_locator(created)
    recorded = {
        "app_id": required["app_id"],
        "app_slug": required["app_slug"],
        "binding_digest": binding_digest,
        "check_run_node_id": locator["check_run_node_id"],
        "check_run_id": check_run_id,
        "check_suite_id": locator["check_suite_id"],
        "completed_at": created["completed_at"],
        "context": required["context"],
        "details_url": canonical_details_url,
        "external_id": external_id,
        "head_sha": config.release_commit,
        "historical_check_run_id": historical["id"],
        "lanes": list(lane_jobs),
        "required_pull_request": contract["sync"]["pull_request"],
    }
    _verify_published_recovery_check(
        client,
        config,
        contract,
        attestation,
        artifact,
        lane_jobs,
        recorded,
        pull_request_state="OPEN",
        work_actor=work_actor,
    )
    return recorded


def _recovery_workflow_run(
    client: GitHubClient,
    config: SyncConfig,
    run_id: int,
    run_attempt: int,
    activation_commit: str,
    *,
    completed: bool,
    expected_conclusion: str = "success",
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
        if (
            run.get("status") != "completed"
            or run.get("conclusion") != expected_conclusion
        ):
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
    expected_conclusion: str = "success",
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
        or selected.get("conclusion") != expected_conclusion
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
    expected_expires_at: str | None = None,
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
    if expected_expires_at is not None:
        expires_at = _timestamp("Retained artifact expiry", artifact.get("expires_at"))
        if artifact.get("expires_at") != expected_expires_at:
            raise ApprovalError("Retained preflight artifact expiry changed")
        if expires_at <= dt.datetime.now(dt.timezone.utc):
            raise ApprovalError("Retained preflight artifact has expired")
    return artifact


def _verify_failed_recovery_validation(
    client: GitHubClient,
    config: SyncConfig,
    contract: Mapping[str, Any],
    *,
    work_actor: str,
) -> dict[str, Any]:
    """Preserve the exact failed attempt without trusting its display title."""

    expected = contract["failed_validation"]
    run_id = expected["run_id"]
    attempt = expected["run_attempt"]
    activation_commit = contract["activation"]["commit"]
    run = client.get(_path(config.repository, f"actions/runs/{run_id}"))
    if (
        not isinstance(run, dict)
        or run.get("id") != run_id
        or run.get("run_attempt") != attempt
        or run.get("status") != "completed"
        or run.get("conclusion") != "failure"
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
        raise ApprovalError("Failed recovery validation provenance changed")
    payload = client.get(
        _path(config.repository, f"actions/runs/{run_id}/jobs"),
        {"filter": "latest", "per_page": "100"},
    )
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    total = payload.get("total_count") if isinstance(payload, dict) else None
    if not isinstance(jobs, list) or total != len(jobs) or len(jobs) > 100:
        raise ApprovalError("Failed recovery validation job list is malformed")
    normalized = []
    for job in jobs:
        if (
            not isinstance(job, dict)
            or job.get("run_id") != run_id
            or job.get("run_attempt") != attempt
            or job.get("head_sha") != activation_commit
            or job.get("status") != "completed"
        ):
            raise ApprovalError("Failed recovery validation job identity changed")
        normalized.append(
            {
                "conclusion": job.get("conclusion"),
                "id": _safe_int(
                    "Failed recovery validation job ID", job.get("id")
                ),
                "name": job.get("name"),
            }
        )
    if normalized != expected["jobs"]:
        raise ApprovalError("Failed recovery validation job evidence changed")
    artifact = expected["artifact"]
    return _artifact(
        client,
        config,
        run_id,
        artifact["name"],
        head_sha=activation_commit,
        expected_id=artifact["id"],
        expected_digest=artifact["digest"],
    )


def _verify_failed_recovery_publication(
    client: GitHubClient,
    config: SyncConfig,
    contract: Mapping[str, Any],
    *,
    work_actor: str,
) -> dict[str, Any]:
    """Preserve the exact all-green run whose final publication failed."""

    expected = contract["failed_publication"]
    run_id = expected["run_id"]
    attempt = expected["run_attempt"]
    continuation_commit = contract["continuation"]["commit"]
    run = client.get(_path(config.repository, f"actions/runs/{run_id}"))
    if (
        not isinstance(run, dict)
        or run.get("id") != run_id
        or run.get("run_attempt") != attempt
        or run.get("status") != "completed"
        or run.get("conclusion") != "failure"
        or run.get("event") != "workflow_dispatch"
        or run.get("head_sha") != continuation_commit
        or run.get("head_branch") != "main"
        or _workflow_path(run) != validation_recovery.WORKFLOW_PATH
        or _repo_name(run.get("repository")) != config.repository
        or _repo_name(run.get("head_repository")) != config.repository
        or not _allowed_operator(_login(run.get("actor")), config, work_actor)
        or not _allowed_operator(
            _login(run.get("triggering_actor")), config, work_actor
        )
    ):
        raise ApprovalError("Failed recovery publication provenance changed")
    payload = client.get(
        _path(config.repository, f"actions/runs/{run_id}/jobs"),
        {"filter": "latest", "per_page": "100"},
    )
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    total = payload.get("total_count") if isinstance(payload, dict) else None
    if not isinstance(jobs, list) or total != len(jobs) or len(jobs) > 100:
        raise ApprovalError("Failed recovery publication job list is malformed")
    normalized = []
    for job in jobs:
        if (
            not isinstance(job, dict)
            or job.get("run_id") != run_id
            or job.get("run_attempt") != attempt
            or job.get("head_sha") != continuation_commit
            or job.get("status") != "completed"
        ):
            raise ApprovalError("Failed recovery publication job identity changed")
        normalized.append(
            {
                "conclusion": job.get("conclusion"),
                "id": _safe_int("Failed recovery publication job ID", job.get("id")),
                "name": job.get("name"),
            }
        )
    if normalized != expected["jobs"]:
        raise ApprovalError("Failed recovery publication job evidence changed")
    artifact = expected["artifact"]
    return _artifact(
        client,
        config,
        run_id,
        artifact["name"],
        head_sha=continuation_commit,
        expected_id=artifact["id"],
        expected_digest=artifact["digest"],
    )


def _verify_partial_recovery_publication(
    client: GitHubClient,
    config: SyncConfig,
    contract: Mapping[str, Any],
    attestation: Mapping[str, Any],
    *,
    pull_request_state: str,
    work_actor: str,
    require_current_pr: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Verify and reuse the exact successful check from the failed final step."""

    expected = contract["partial_publication"]
    run_id = expected["run_id"]
    attempt = expected["run_attempt"]
    repair_commit = contract["publication_repair"]["base_commit"]
    _recovery_workflow_run(
        client,
        config,
        run_id,
        attempt,
        repair_commit,
        completed=True,
        expected_conclusion="failure",
        work_actor=work_actor,
    )
    _verify_newest_recovery_run(
        client,
        config,
        repair_commit,
        run_id,
        attempt,
        completed=True,
        expected_conclusion="failure",
        work_actor=work_actor,
    )
    payload = client.get(
        _path(config.repository, f"actions/runs/{run_id}/jobs"),
        {"filter": "latest", "per_page": "100"},
    )
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    total = payload.get("total_count") if isinstance(payload, dict) else None
    if not isinstance(jobs, list) or total != len(jobs) or len(jobs) > 100:
        raise ApprovalError("Partial recovery publication job list is malformed")
    normalized = []
    for job in jobs:
        if (
            not isinstance(job, dict)
            or job.get("run_id") != run_id
            or job.get("run_attempt") != attempt
            or job.get("head_sha") != repair_commit
            or job.get("status") != "completed"
        ):
            raise ApprovalError("Partial recovery publication job identity changed")
        normalized.append(
            {
                "conclusion": job.get("conclusion"),
                "id": _safe_int("Partial recovery publication job ID", job.get("id")),
                "name": job.get("name"),
            }
        )
    if normalized != expected["jobs"]:
        raise ApprovalError("Partial recovery publication job evidence changed")
    actual_attestation_sha256 = "sha256:" + hashlib.sha256(
        validation_recovery.canonical_json_bytes(attestation)
    ).hexdigest()
    if actual_attestation_sha256 != expected["attestation_sha256"]:
        raise ApprovalError("Partial recovery attestation bytes changed")
    artifact_contract = expected["artifact"]
    artifact = _artifact(
        client,
        config,
        run_id,
        artifact_contract["name"],
        head_sha=repair_commit,
        expected_id=artifact_contract["id"],
        expected_digest=artifact_contract["digest"],
    )
    lane_jobs = _recovery_lane_jobs(
        client,
        config,
        contract,
        run_id=run_id,
        run_attempt=attempt,
        activation_commit=repair_commit,
    )
    required_check = {**expected["required_check"], "lanes": lane_jobs}
    _verify_published_recovery_check(
        client,
        config,
        contract,
        attestation,
        artifact,
        lane_jobs,
        required_check,
        pull_request_state=pull_request_state,
        work_actor=work_actor,
        require_current_pr=require_current_pr,
    )
    return artifact, lane_jobs, required_check


def _feature_readiness(
    value: Any,
    config: SyncConfig,
    *,
    merge_source_commit: str | None = None,
    expected_pull_request: int | None = None,
    expected_feature_head: str | None = None,
) -> dict[str, Any]:
    expected_merge = merge_source_commit or config.source_sha
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
        or value.get("merge_source_commit") != expected_merge
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
    if expected_pull_request is not None and feature_number != expected_pull_request:
        raise ApprovalError("Feature-readiness pull request changed")
    if mode == "first-introduction-postmerge" and feature_number != 44:
        raise ApprovalError("First-introduction readiness is restricted to pull request 44")
    feature_head = feature_pr.get("head_sha")
    if (
        not isinstance(feature_head, str)
        or COMMIT_RE.fullmatch(feature_head) is None
        or feature_head == expected_merge
        or expected_feature_head is not None
        and feature_head != expected_feature_head
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
        "merge_source_commit": expected_merge,
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
    payload = {
        "approval_nonce": ready["approval_nonce"],
        "platform_version": ready["platform_version"],
        "release_commit": ready["release_commit"],
        "schema_version": ready["schema_version"],
    }
    if ready.get("schema_version") == CURRENT_RECOVERY_SCHEMA_VERSION:
        payload.update(
            {
                "sync_base_commit": ready["sync_base_commit"],
                "sync_head_commit": ready["sync_head_commit"],
            }
        )
    return payload


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
    current_recovery = (
        recovery and ready.get("schema_version") == CURRENT_RECOVERY_SCHEMA_VERSION
    )
    ready_prefix = (
        CURRENT_RECOVERY_READY_PREFIX
        if current_recovery
        else RECOVERY_READY_PREFIX
        if recovery
        else READY_PREFIX
    )
    approval_prefix = (
        CURRENT_RECOVERY_APPROVAL_PREFIX
        if current_recovery
        else RECOVERY_APPROVAL_PREFIX
        if recovery
        else APPROVAL_PREFIX
    )
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
    activation_readiness: Mapping[str, Any],
    continuation_readiness: Mapping[str, Any],
    repair_readiness: Mapping[str, Any],
    publication_repair: Mapping[str, Any],
    publication_readiness: Mapping[str, Any],
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
    activation = attestation["activation"]
    continuation = attestation["continuation"]
    repair = attestation["repair"]
    activation_commit = activation["activation_commit"]
    continuation_commit = continuation["continuation_commit"]
    repair_commit = repair["repair_commit"]
    published_activation = _feature_readiness(
        activation_readiness,
        config,
        merge_source_commit=activation_commit,
        expected_pull_request=activation["pull_request"],
        expected_feature_head=activation["feature_head"],
    )
    published_continuation = _feature_readiness(
        continuation_readiness,
        config,
        merge_source_commit=continuation_commit,
        expected_pull_request=continuation["pull_request"],
        expected_feature_head=continuation["feature_head"],
    )
    published_repair = _feature_readiness(
        repair_readiness,
        config,
        merge_source_commit=repair_commit,
        expected_pull_request=repair["pull_request"],
        expected_feature_head=repair["feature_head"],
    )
    publication = _publication_repair_evidence(publication_repair, contract)
    publication_commit = publication["publication_repair_commit"]
    published_publication = _feature_readiness(
        publication_readiness,
        config,
        merge_source_commit=publication_commit,
        expected_pull_request=publication["pull_request"],
        expected_feature_head=publication["feature_head"],
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
    partial_publication = contract["partial_publication"]
    partial_failures = [
        item
        for item in partial_publication["jobs"]
        if item.get("conclusion") == "failure"
    ]
    if len(partial_failures) != 1:
        raise ApprovalError("Partial recovery publication failure changed")
    partial_failed_job_id = _safe_int(
        "Partial recovery publication failed job ID",
        partial_failures[0].get("id"),
    )
    expected_validation_artifact = partial_publication["artifact"]["name"]
    artifact_id = _safe_int(
        "Recovery validation artifact ID", validation_artifact_id
    )
    if DIGEST_RE.fullmatch(validation_artifact_digest) is None:
        raise ApprovalError("Recovery validation artifact digest is invalid")
    if validation_artifact_digest != partial_publication["artifact"]["digest"]:
        raise ApprovalError("Recovery validation artifact digest changed")
    if (
        validation_run_id != partial_publication["run_id"]
        or validation_attempt != partial_publication["run_attempt"]
        or validation_artifact_name != expected_validation_artifact
        or artifact_id != partial_publication["artifact"]["id"]
    ):
        raise ApprovalError("Recovery validation artifact name is invalid")

    client = GitHubClient(
        config.api_url, token, transport=transport, sleeper=sleeper
    )
    _get_pr(client, config, sync["pull_request"], "open")
    if _read_ref(client, config, "main") != publication_commit:
        raise ApprovalError("Recovery publication repair is not the current main tip")
    _verify_release(client, config, sync=True)
    _verify_activation_remote(
        client,
        config,
        attestation,
        state="closed",
        work_actor=work_actor,
    )
    _verify_continuation_remote(
        client,
        config,
        attestation,
        state="closed",
        work_actor=work_actor,
    )
    _verify_repair_remote(
        client,
        config,
        attestation,
        state="closed",
        work_actor=work_actor,
    )
    _verify_publication_repair_remote(
        client,
        config,
        publication,
        state="closed",
        work_actor=work_actor,
    )
    activation_validation = contract["activation"]["validation"]
    _verify_main_run_for_commit(
        client,
        config,
        activation_commit,
        activation_validation["run_id"],
        activation_validation["run_attempt"],
    )
    continuation_validation = contract["continuation"]["validation"]
    _verify_main_run_for_commit(
        client,
        config,
        continuation_commit,
        continuation_validation["run_id"],
        continuation_validation["run_attempt"],
    )
    repair_run = _main_ci_run(client, config, repair_commit)
    publication_run = _main_ci_run(client, config, publication_commit)
    _verify_main_run(
        client,
        config,
        contract["main_validation"]["run_id"],
        contract["main_validation"]["run_attempt"],
    )
    _verify_recovery_preflight(
        client, config, contract, work_actor=work_actor
    )
    failed_validation_artifact = _verify_failed_recovery_validation(
        client, config, contract, work_actor=work_actor
    )
    failed_publication_artifact = _verify_failed_recovery_publication(
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
    validation_artifact, lane_jobs, required_check = (
        _verify_partial_recovery_publication(
            client,
            config,
            contract,
            attestation,
            pull_request_state="OPEN",
            work_actor=work_actor,
        )
    )

    # Repeat every mutable boundary immediately before posting the one approval
    # request. Both prior failed runs are accepted only through their exact jobs.
    if _read_ref(client, config, "main") != publication_commit:
        raise ApprovalError("Recovery publication repair moved before readiness publication")
    _verify_release(client, config, sync=True)
    _verify_main_run_for_commit(
        client,
        config,
        repair_commit,
        repair_run["id"],
        repair_run["run_attempt"],
    )
    _verify_main_run_for_commit(
        client,
        config,
        publication_commit,
        publication_run["id"],
        publication_run["run_attempt"],
    )
    _verify_recovery_preflight(
        client, config, contract, work_actor=work_actor
    )
    failed_validation_artifact = _verify_failed_recovery_validation(
        client, config, contract, work_actor=work_actor
    )
    failed_publication_artifact = _verify_failed_recovery_publication(
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
    final_artifact, final_lane_jobs, final_required_check = (
        _verify_partial_recovery_publication(
            client,
            config,
            contract,
            attestation,
            pull_request_state="OPEN",
            work_actor=work_actor,
        )
    )
    if (
        final_artifact["id"] != validation_artifact["id"]
        or final_artifact["digest"] != validation_artifact["digest"]
        or final_lane_jobs != lane_jobs
        or final_required_check != required_check
    ):
        raise ApprovalError("Partial recovery publication changed before readiness")
    existing = []
    for item in _comments(client, config, sync["pull_request"]):
        normal = _marker_payload(item.get("body"), READY_PREFIX)
        recovered = _marker_payload(item.get("body"), RECOVERY_READY_PREFIX)
        if normal is not None or recovered is not None:
            existing.append(item)
    if existing:
        raise ApprovalError("A readiness marker already exists for this pull request")

    if _read_ref(client, config, "main") != publication_commit:
        raise ApprovalError("Recovery publication repair moved after check verification")
    _verify_release(client, config, sync=True)
    _get_pr(client, config, sync["pull_request"], "open")
    validation_artifact = final_artifact
    if any(
        _marker_payload(item.get("body"), READY_PREFIX) is not None
        or _marker_payload(item.get("body"), RECOVERY_READY_PREFIX) is not None
        for item in _comments(client, config, sync["pull_request"])
    ):
        raise ApprovalError("A readiness marker appeared during check publication")

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
            "activation_readiness": published_activation,
            "activation_validation_run_attempt": activation_validation["run_attempt"],
            "activation_validation_run_id": activation_validation["run_id"],
            "artifact_digest": validation_artifact["digest"],
            "artifact_id": validation_artifact["id"],
            "artifact_name": validation_artifact_name,
            "attestation": attestation,
            "continuation_readiness": published_continuation,
            "continuation_validation_run_attempt": continuation_validation[
                "run_attempt"
            ],
            "continuation_validation_run_id": continuation_validation["run_id"],
            "failed_publication": {
                "artifact_digest": failed_publication_artifact["digest"],
                "artifact_id": failed_publication_artifact["id"],
                "artifact_name": failed_publication_artifact["name"],
                "run_attempt": contract["failed_publication"]["run_attempt"],
                "run_id": contract["failed_publication"]["run_id"],
            },
            "failed_validation": {
                "artifact_digest": failed_validation_artifact["digest"],
                "artifact_id": failed_validation_artifact["id"],
                "artifact_name": failed_validation_artifact["name"],
                "run_attempt": contract["failed_validation"]["run_attempt"],
                "run_id": contract["failed_validation"]["run_id"],
            },
            "mode": "published-release-recovery",
            "partial_publication": {
                "artifact_digest": validation_artifact["digest"],
                "artifact_id": validation_artifact["id"],
                "artifact_name": validation_artifact["name"],
                "failed_job_id": partial_failed_job_id,
                "run_attempt": validation_attempt,
                "run_id": validation_run_id,
            },
            "publication_repair": publication,
            "publication_repair_readiness": published_publication,
            "publication_repair_validation_run_attempt": publication_run[
                "run_attempt"
            ],
            "publication_repair_validation_run_id": publication_run["id"],
            "repair_readiness": published_repair,
            "repair_validation_run_attempt": repair_run["run_attempt"],
            "repair_validation_run_id": repair_run["id"],
            "required_check": required_check,
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


def ready_recovery_update(
    contract: Mapping[str, Any],
    token: str,
    *,
    owner: str,
    repository: str,
    api_url: str,
    server_url: str,
    output: Path,
    sync_update: Mapping[str, Any],
    sync_repair_readiness: Mapping[str, Any],
    work_actor: str = "",
    transport=None,
    sleeper: Callable[[float], None] = time.sleep,
    polls: int = SOURCE_CI_POLLS,
) -> dict[str, Any]:
    """Supersede stale schema-7 readiness after one reviewed PR update."""

    release = contract["release"]
    sync = contract["sync"]
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
    update = _sync_update_evidence(sync_update, contract)
    repair = update["sync_repair"]
    if update["sync_head_commit"] == release["commit"]:
        raise ApprovalError("Updated synchronization head still aliases the release commit")
    published_repair = _feature_readiness(
        sync_repair_readiness,
        config,
        merge_source_commit=repair["sync_repair_commit"],
        expected_pull_request=repair["pull_request"],
        expected_feature_head=repair["feature_head"],
    )
    client = GitHubClient(config.api_url, token, transport=transport, sleeper=sleeper)
    comments = _comments(client, config, sync["pull_request"])
    historical_comment, historical_payload, historical_evidence = (
        _historical_recovery_comment(comments, config, contract)
    )
    if any(
        _marker_payload(item.get("body"), READY_PREFIX) is not None
        or _marker_payload(item.get("body"), CURRENT_RECOVERY_READY_PREFIX) is not None
        for item in comments
    ):
        raise ApprovalError("A current readiness marker already exists for this pull request")
    if _read_ref(client, config, "main") != repair["sync_repair_commit"]:
        raise ApprovalError("Reviewed synchronization base is not current main")
    _verify_release(client, config, sync=False)
    _verify_sync_repair_remote(client, config, repair, work_actor=work_actor)
    repair_run = _main_ci_run(client, config, repair["sync_repair_commit"])
    _verify_main_run_for_commit(
        client,
        config,
        repair["sync_repair_commit"],
        repair_run["id"],
        repair_run["run_attempt"],
    )
    _verify_sync_update_remote(client, config, contract, update)
    _get_pr(
        client,
        config,
        sync["pull_request"],
        "open",
        expected_head=update["sync_head_commit"],
        expected_base=repair["sync_repair_commit"],
        require_mergeable=True,
    )
    _verify_historical_recovery_remote(
        client,
        config,
        contract,
        historical_evidence,
        work_actor=work_actor,
    )
    source_run = _source_ci_run(
        client,
        config,
        sync["pull_request"],
        sleeper=sleeper,
        polls=polls,
        head_commit=update["sync_head_commit"],
    )
    native_check = _verify_native_sync_validation(
        client,
        config,
        contract,
        number=sync["pull_request"],
        head_commit=update["sync_head_commit"],
        run_id=source_run["id"],
        run_attempt=source_run["run_attempt"],
        pull_request_state="OPEN",
    )

    # Re-read every mutable edge after the potentially long native-check poll.
    if _read_ref(client, config, "main") != repair["sync_repair_commit"]:
        raise ApprovalError("Main advanced before updated recovery readiness")
    _verify_release(client, config, sync=False)
    _verify_sync_update_remote(client, config, contract, update)
    _get_pr(
        client,
        config,
        sync["pull_request"],
        "open",
        expected_head=update["sync_head_commit"],
        expected_base=repair["sync_repair_commit"],
        require_mergeable=True,
    )
    final_comments = _comments(client, config, sync["pull_request"])
    final_historical_comment, final_historical_payload, _ = (
        _historical_recovery_comment(final_comments, config, contract)
    )
    if (
        final_historical_comment != historical_comment
        or final_historical_payload != historical_payload
        or any(
            _marker_payload(item.get("body"), READY_PREFIX) is not None
            or _marker_payload(item.get("body"), CURRENT_RECOVERY_READY_PREFIX)
            is not None
            for item in final_comments
        )
    ):
        raise ApprovalError("Readiness history changed during current publication")
    native_check = _verify_native_sync_validation(
        client,
        config,
        contract,
        number=sync["pull_request"],
        head_commit=update["sync_head_commit"],
        run_id=source_run["id"],
        run_attempt=source_run["run_attempt"],
        pull_request_state="OPEN",
    )
    fields: dict[str, Any] = {
        "feature_readiness": historical_payload["feature_readiness"],
        "main_validation_run_attempt": historical_payload[
            "main_validation_run_attempt"
        ],
        "main_validation_run_id": historical_payload["main_validation_run_id"],
        "manifest_sha256": release["manifest_sha256"],
        "platform_version": contract["platform_version"],
        "preflight_artifact": historical_payload["preflight_artifact"],
        "preflight_artifact_digest": historical_payload[
            "preflight_artifact_digest"
        ],
        "preflight_artifact_id": historical_payload["preflight_artifact_id"],
        "preflight_run_attempt": historical_payload["preflight_run_attempt"],
        "preflight_run_id": historical_payload["preflight_run_id"],
        "pull_request": sync["pull_request"],
        "recovery_validation": {
            "mode": "published-release-sync-update",
            "native_required_check": native_check,
            "sync_repair": repair,
            "sync_repair_readiness": published_repair,
            "sync_repair_validation_run_attempt": repair_run["run_attempt"],
            "sync_repair_validation_run_id": repair_run["id"],
        },
        "release_commit": release["commit"],
        "repository": contract["repository"],
        "schema_version": CURRENT_RECOVERY_SCHEMA_VERSION,
        "source_commit": release["source_commit"],
        "supersedes": dict(contract["historical_readiness"]),
        "sync_base_commit": repair["sync_repair_commit"],
        "sync_head_commit": update["sync_head_commit"],
        "sync_head_tree": update["sync_head_tree"],
        "sync_validation_run_attempt": source_run["run_attempt"],
        "sync_validation_run_id": source_run["id"],
    }
    fields["approval_nonce"] = _nonce(fields)
    comment = _post_ready_comment(
        client, config, sync["pull_request"], fields, recovery=True
    )
    return {
        "approval_marker": marker(
            CURRENT_RECOVERY_APPROVAL_PREFIX, approval_payload(fields)
        ),
        "comment_id": comment["id"],
        "ready": fields,
        "schema_version": CURRENT_RECOVERY_SCHEMA_VERSION,
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
CURRENT_RECOVERY_READY_KEYS = RECOVERY_READY_KEYS | {
    "supersedes",
    "sync_base_commit",
    "sync_head_commit",
    "sync_head_tree",
}


def _validate_ready_payload(payload: Mapping[str, Any], config: SyncConfig, number: int):
    if payload.get("schema_version") == CURRENT_RECOVERY_SCHEMA_VERSION:
        return _validate_current_recovery_ready_payload(payload, config, number)
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
        "activation_readiness",
        "activation_validation_run_attempt",
        "activation_validation_run_id",
        "artifact_digest",
        "artifact_id",
        "artifact_name",
        "attestation",
        "continuation_readiness",
        "continuation_validation_run_attempt",
        "continuation_validation_run_id",
        "failed_publication",
        "failed_validation",
        "mode",
        "partial_publication",
        "publication_repair",
        "publication_repair_readiness",
        "publication_repair_validation_run_attempt",
        "publication_repair_validation_run_id",
        "repair_readiness",
        "repair_validation_run_attempt",
        "repair_validation_run_id",
        "required_check",
    }:
        raise ApprovalError("Recovery validation evidence schema is invalid")
    if recovery.get("mode") != "published-release-recovery":
        raise ApprovalError("Recovery validation mode is invalid")
    attestation = validation_recovery.validate_attestation_data(
        recovery.get("attestation"), contract=contract
    )
    activation = attestation["activation"]
    continuation = attestation["continuation"]
    repair = attestation["repair"]
    activation_readiness = _feature_readiness(
        recovery.get("activation_readiness"),
        config,
        merge_source_commit=activation["activation_commit"],
        expected_pull_request=activation["pull_request"],
        expected_feature_head=activation["feature_head"],
    )
    continuation_readiness = _feature_readiness(
        recovery.get("continuation_readiness"),
        config,
        merge_source_commit=continuation["continuation_commit"],
        expected_pull_request=continuation["pull_request"],
        expected_feature_head=continuation["feature_head"],
    )
    repair_readiness = _feature_readiness(
        recovery.get("repair_readiness"),
        config,
        merge_source_commit=repair["repair_commit"],
        expected_pull_request=repair["pull_request"],
        expected_feature_head=repair["feature_head"],
    )
    publication = _publication_repair_evidence(
        recovery.get("publication_repair"), contract
    )
    publication_readiness = _feature_readiness(
        recovery.get("publication_repair_readiness"),
        config,
        merge_source_commit=publication["publication_repair_commit"],
        expected_pull_request=publication["pull_request"],
        expected_feature_head=publication["feature_head"],
    )
    publication_run_id = _safe_int(
        "Publication repair Validate PR run ID",
        recovery.get("publication_repair_validation_run_id"),
    )
    publication_attempt = _safe_int(
        "Publication repair Validate PR run attempt",
        recovery.get("publication_repair_validation_run_attempt"),
        maximum=10**6,
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
    expected_partial = contract["partial_publication"]
    partial_failures = [
        item
        for item in expected_partial["jobs"]
        if item.get("conclusion") == "failure"
    ]
    if len(partial_failures) != 1:
        raise ApprovalError("Partial recovery publication failure changed")
    partial_failed_job_id = _safe_int(
        "Partial recovery publication failed job ID",
        partial_failures[0].get("id"),
    )
    expected_name = expected_partial["artifact"]["name"]
    if (
        validation_run_id != expected_partial["run_id"]
        or validation_attempt != expected_partial["run_attempt"]
        or artifact_id != expected_partial["artifact"]["id"]
        or artifact_digest != expected_partial["artifact"]["digest"]
        or recovery.get("artifact_name") != expected_name
    ):
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
    if (
        activation_run_id != contract["activation"]["validation"]["run_id"]
        or activation_attempt
        != contract["activation"]["validation"]["run_attempt"]
    ):
        raise ApprovalError("Recovery activation validation evidence changed")
    continuation_run_id = _safe_int(
        "Continuation Validate PR run ID",
        recovery.get("continuation_validation_run_id"),
    )
    continuation_attempt = _safe_int(
        "Continuation Validate PR run attempt",
        recovery.get("continuation_validation_run_attempt"),
        maximum=10**6,
    )
    if (
        continuation_run_id != contract["continuation"]["validation"]["run_id"]
        or continuation_attempt
        != contract["continuation"]["validation"]["run_attempt"]
    ):
        raise ApprovalError("Recovery continuation validation evidence changed")
    repair_run_id = _safe_int(
        "Digest repair Validate PR run ID",
        recovery.get("repair_validation_run_id"),
    )
    repair_attempt = _safe_int(
        "Digest repair Validate PR run attempt",
        recovery.get("repair_validation_run_attempt"),
        maximum=10**6,
    )
    partial_publication = recovery.get("partial_publication")
    if not isinstance(partial_publication, dict) or partial_publication != {
        "artifact_digest": expected_partial["artifact"]["digest"],
        "artifact_id": expected_partial["artifact"]["id"],
        "artifact_name": expected_partial["artifact"]["name"],
        "failed_job_id": partial_failed_job_id,
        "run_attempt": expected_partial["run_attempt"],
        "run_id": expected_partial["run_id"],
    }:
        raise ApprovalError("Partial recovery publication evidence changed")
    failed_publication = recovery.get("failed_publication")
    expected_publication = contract["failed_publication"]
    if not isinstance(failed_publication, dict) or failed_publication != {
        "artifact_digest": expected_publication["artifact"]["digest"],
        "artifact_id": expected_publication["artifact"]["id"],
        "artifact_name": expected_publication["artifact"]["name"],
        "run_attempt": expected_publication["run_attempt"],
        "run_id": expected_publication["run_id"],
    }:
        raise ApprovalError("Failed recovery publication evidence changed")
    failed_validation = recovery.get("failed_validation")
    expected_failed = contract["failed_validation"]
    if not isinstance(failed_validation, dict) or failed_validation != {
        "artifact_digest": expected_failed["artifact"]["digest"],
        "artifact_id": expected_failed["artifact"]["id"],
        "artifact_name": expected_failed["artifact"]["name"],
        "run_attempt": expected_failed["run_attempt"],
        "run_id": expected_failed["run_id"],
    }:
        raise ApprovalError("Failed recovery validation evidence changed")
    required_evidence = recovery.get("required_check")
    if not isinstance(required_evidence, dict) or set(required_evidence) != {
        "app_id",
        "app_slug",
        "binding_digest",
        "check_run_node_id",
        "check_run_id",
        "check_suite_id",
        "completed_at",
        "context",
        "details_url",
        "external_id",
        "head_sha",
        "historical_check_run_id",
        "lanes",
        "required_pull_request",
    }:
        raise ApprovalError("Recovery required-check evidence schema is invalid")
    required = _required_check_contract(contract)
    lanes = required_evidence.get("lanes")
    if not isinstance(lanes, list) or len(lanes) != len(required["required_lanes"]):
        raise ApprovalError("Recovery required-check lanes are invalid")
    normalized_lanes = []
    for lane, value in zip(required["required_lanes"], lanes, strict=True):
        expected_job_name = f"{RECOVERY_SOURCE_JOB_PREFIX} / {lane}"
        if (
            not isinstance(value, dict)
            or set(value) != {"conclusion", "id", "name"}
            or value.get("conclusion") != "success"
            or value.get("name") != expected_job_name
        ):
            raise ApprovalError("Recovery required-check lane identity is invalid")
        normalized_lanes.append(
            {
                "conclusion": "success",
                "id": _safe_int("Recovery validation job ID", value.get("id")),
                "name": expected_job_name,
            }
        )
    artifact = {
        "digest": artifact_digest,
        "id": artifact_id,
        "name": expected_name,
    }
    _, binding_digest, external_id = _recovery_check_payload(
        config, contract, attestation, artifact, normalized_lanes
    )
    check_run_id = _safe_int(
        "Recovered required check ID", required_evidence.get("check_run_id")
    )
    expected_details_url = _canonical_check_url(config, check_run_id)
    _safe_int(
        "Recovered required check suite ID",
        required_evidence.get("check_suite_id"),
    )
    _opaque_check_node_id(required_evidence.get("check_run_node_id"))
    completed_at = required_evidence.get("completed_at")
    _timestamp("Recovered required check completion", completed_at)
    if (
        required_evidence.get("app_id") != required["app_id"]
        or required_evidence.get("app_slug") != required["app_slug"]
        or required_evidence.get("binding_digest") != binding_digest
        or required_evidence.get("context") != required["context"]
        or required_evidence.get("details_url") != expected_details_url
        or required_evidence.get("external_id") != external_id
        or required_evidence.get("head_sha") != config.release_commit
        or required_evidence.get("historical_check_run_id")
        != required["historical_failure"]["check_run_id"]
        or required_evidence.get("lanes") != normalized_lanes
        or required_evidence.get("required_pull_request")
        != contract["sync"]["pull_request"]
    ):
        raise ApprovalError("Recovery required-check evidence changed")
    if required_evidence != {
        **contract["partial_publication"]["required_check"],
        "lanes": normalized_lanes,
    }:
        raise ApprovalError("Partial recovery required-check evidence changed")
    if payload["approval_nonce"] != _nonce(payload):
        raise ApprovalError("Recovery readiness marker nonce is invalid")
    return {
        "activation_attempt": activation_attempt,
        "activation_commit": activation["activation_commit"],
        "activation_readiness": activation_readiness,
        "activation_run_id": activation_run_id,
        "artifact_digest": artifact_digest,
        "artifact_id": artifact_id,
        "artifact_name": expected_name,
        "attestation": attestation,
        "continuation_attempt": continuation_attempt,
        "continuation_commit": continuation["continuation_commit"],
        "continuation_readiness": continuation_readiness,
        "continuation_run_id": continuation_run_id,
        "failed_publication": dict(failed_publication),
        "failed_validation": dict(failed_validation),
        "feature_readiness": feature_readiness,
        "main_attempt": main_attempt,
        "main_run_id": main_run_id,
        "mode": "published-release-recovery",
        "partial_publication": dict(partial_publication),
        "preflight_artifact_digest": contract["preflight"]["artifact_digest"],
        "preflight_artifact_id": preflight_artifact_id,
        "preflight_attempt": preflight_attempt,
        "preflight_run_id": preflight_id,
        "publication_attempt": publication_attempt,
        "publication_commit": publication["publication_repair_commit"],
        "publication_readiness": publication_readiness,
        "publication_repair": publication,
        "publication_run_id": publication_run_id,
        "repair_attempt": repair_attempt,
        "repair_commit": repair["repair_commit"],
        "repair_readiness": repair_readiness,
        "repair_run_id": repair_run_id,
        "required_check": dict(required_evidence),
        "required_check_id": check_run_id,
        "required_lanes": normalized_lanes,
        "sync_attempt": validation_attempt,
        "sync_run_id": validation_run_id,
    }


def _historical_recovery_comment(
    comments: Sequence[Mapping[str, Any]],
    config: SyncConfig,
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    historical = contract["historical_readiness"]
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for value in comments:
        payload = _marker_payload(value.get("body"), RECOVERY_READY_PREFIX)
        if payload is not None:
            matches.append((dict(value), payload))
    if len(matches) != 1:
        raise ApprovalError("Exactly one historical recovery readiness marker is required")
    comment, payload = matches[0]
    if (
        _safe_int("Historical readiness comment ID", comment.get("id"))
        != historical["comment_id"]
        or _login(comment.get("user")) != BOT_LOGIN
        or comment.get("created_at") != historical["created_at"]
        or payload.get("schema_version") != historical["schema_version"]
        or payload.get("approval_nonce") != historical["approval_nonce"]
        or payload.get("release_commit") != historical["sync_head_commit"]
    ):
        raise ApprovalError("Historical recovery readiness marker changed")
    evidence = _validate_recovery_ready_payload(
        payload, config, contract["sync"]["pull_request"]
    )
    return comment, payload, evidence


def _verify_historical_recovery_remote(
    client: GitHubClient,
    config: SyncConfig,
    contract: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    work_actor: str,
) -> dict[str, Any]:
    _verify_main_run(
        client, config, evidence["main_run_id"], evidence["main_attempt"]
    )
    _verify_activation_remote(
        client,
        config,
        evidence["attestation"],
        state="closed",
        work_actor=work_actor,
    )
    _verify_continuation_remote(
        client,
        config,
        evidence["attestation"],
        state="closed",
        work_actor=work_actor,
    )
    _verify_repair_remote(
        client,
        config,
        evidence["attestation"],
        state="closed",
        work_actor=work_actor,
    )
    _verify_publication_repair_remote(
        client,
        config,
        evidence["publication_repair"],
        state="closed",
        work_actor=work_actor,
    )
    for commit, run_id, attempt in (
        (
            evidence["activation_commit"],
            evidence["activation_run_id"],
            evidence["activation_attempt"],
        ),
        (
            evidence["continuation_commit"],
            evidence["continuation_run_id"],
            evidence["continuation_attempt"],
        ),
        (
            evidence["repair_commit"],
            evidence["repair_run_id"],
            evidence["repair_attempt"],
        ),
        (
            evidence["publication_commit"],
            evidence["publication_run_id"],
            evidence["publication_attempt"],
        ),
    ):
        _verify_main_run_for_commit(client, config, commit, run_id, attempt)
    _verify_recovery_preflight(client, config, contract, work_actor=work_actor)
    _verify_failed_recovery_validation(client, config, contract, work_actor=work_actor)
    _verify_failed_recovery_publication(client, config, contract, work_actor=work_actor)
    artifact, lanes, check = _verify_partial_recovery_publication(
        client,
        config,
        contract,
        evidence["attestation"],
        pull_request_state="OPEN",
        work_actor=work_actor,
        require_current_pr=False,
    )
    if (
        artifact["id"] != evidence["artifact_id"]
        or artifact["digest"] != evidence["artifact_digest"]
        or lanes != evidence["required_lanes"]
        or check != evidence["required_check"]
    ):
        raise ApprovalError("Historical recovery evidence changed")
    preflight = contract["preflight"]
    return _artifact(
        client,
        config,
        preflight["run_id"],
        preflight["artifact_name"],
        expected_id=preflight["artifact_id"],
        expected_digest=preflight["artifact_digest"],
        expected_expires_at=preflight["expires_at"],
    )


def _validate_current_recovery_ready_payload(
    payload: Mapping[str, Any], config: SyncConfig, number: int
) -> dict[str, Any]:
    contract = validation_recovery.load_contract()
    if set(payload) != CURRENT_RECOVERY_READY_KEYS:
        raise ApprovalError("Current recovery readiness marker schema is invalid")
    release = contract["release"]
    sync = contract["sync"]
    if (
        number != sync["pull_request"]
        or payload.get("schema_version") != CURRENT_RECOVERY_SCHEMA_VERSION
        or payload.get("repository") != config.repository
        or payload.get("pull_request") != number
        or payload.get("platform_version") != config.version
        or payload.get("source_commit") != release["source_commit"]
        or payload.get("release_commit") != release["commit"]
        or payload.get("manifest_sha256") != release["manifest_sha256"]
        or not isinstance(payload.get("sync_base_commit"), str)
        or COMMIT_RE.fullmatch(payload["sync_base_commit"]) is None
        or not isinstance(payload.get("sync_head_commit"), str)
        or COMMIT_RE.fullmatch(payload["sync_head_commit"]) is None
        or not isinstance(payload.get("sync_head_tree"), str)
        or COMMIT_RE.fullmatch(payload["sync_head_tree"]) is None
        or payload.get("supersedes") != contract["historical_readiness"]
        or not isinstance(payload.get("approval_nonce"), str)
        or NONCE_RE.fullmatch(payload["approval_nonce"]) is None
    ):
        raise ApprovalError("Current recovery readiness does not match the release and sync head")
    if (
        payload.get("main_validation_run_id") != contract["main_validation"]["run_id"]
        or payload.get("main_validation_run_attempt")
        != contract["main_validation"]["run_attempt"]
    ):
        raise ApprovalError("Current recovery main validation evidence changed")
    preflight = contract["preflight"]
    if (
        payload.get("preflight_run_id") != preflight["run_id"]
        or payload.get("preflight_run_attempt") != preflight["run_attempt"]
        or payload.get("preflight_artifact") != preflight["artifact_name"]
        or payload.get("preflight_artifact_id") != preflight["artifact_id"]
        or payload.get("preflight_artifact_digest") != preflight["artifact_digest"]
    ):
        raise ApprovalError("Current recovery preflight evidence changed")
    feature_readiness = _feature_readiness(payload.get("feature_readiness"), config)
    recovery = payload.get("recovery_validation")
    expected_recovery_keys = {
        "mode",
        "native_required_check",
        "sync_repair",
        "sync_repair_readiness",
        "sync_repair_validation_run_attempt",
        "sync_repair_validation_run_id",
    }
    if not isinstance(recovery, dict) or set(recovery) != expected_recovery_keys:
        raise ApprovalError("Current recovery validation evidence schema is invalid")
    if recovery.get("mode") != "published-release-sync-update":
        raise ApprovalError("Current recovery validation mode is invalid")
    sync_repair = _sync_repair_evidence(recovery.get("sync_repair"), contract)
    if payload["sync_base_commit"] != sync_repair["sync_repair_commit"]:
        raise ApprovalError("Current recovery synchronization base changed")
    repair_readiness = _feature_readiness(
        recovery.get("sync_repair_readiness"),
        config,
        merge_source_commit=sync_repair["sync_repair_commit"],
        expected_pull_request=sync_repair["pull_request"],
        expected_feature_head=sync_repair["feature_head"],
    )
    repair_run_id = _safe_int(
        "Sync-head repair Validate PR run ID",
        recovery.get("sync_repair_validation_run_id"),
    )
    repair_attempt = _safe_int(
        "Sync-head repair Validate PR run attempt",
        recovery.get("sync_repair_validation_run_attempt"),
        maximum=10**6,
    )
    sync_run_id = _safe_int(
        "Updated sync Validate PR run ID", payload.get("sync_validation_run_id")
    )
    sync_attempt = _safe_int(
        "Updated sync Validate PR run attempt",
        payload.get("sync_validation_run_attempt"),
        maximum=10**6,
    )
    native = recovery.get("native_required_check")
    native_keys = {
        "app_id",
        "app_slug",
        "check_run_id",
        "check_run_node_id",
        "check_suite_id",
        "completed_at",
        "context",
        "details_url",
        "head_sha",
        "run_attempt",
        "run_id",
    }
    required = _required_check_contract(contract)
    if (
        not isinstance(native, dict)
        or set(native) != native_keys
        or native.get("app_id") != required["app_id"]
        or native.get("app_slug") != required["app_slug"]
        or native.get("context") != required["context"]
        or native.get("head_sha") != payload["sync_head_commit"]
        or native.get("run_id") != sync_run_id
        or native.get("run_attempt") != sync_attempt
    ):
        raise ApprovalError("Current native required-check evidence changed")
    _safe_int("Current native check ID", native.get("check_run_id"))
    _safe_int("Current native check suite ID", native.get("check_suite_id"))
    _opaque_check_node_id(native.get("check_run_node_id"))
    _timestamp("Current native check completion", native.get("completed_at"))
    if payload["approval_nonce"] != _nonce(payload):
        raise ApprovalError("Current recovery readiness marker nonce is invalid")
    return {
        "feature_readiness": feature_readiness,
        "main_attempt": contract["main_validation"]["run_attempt"],
        "main_run_id": contract["main_validation"]["run_id"],
        "mode": "published-release-sync-update",
        "native_required_check": dict(native),
        "preflight_artifact_digest": preflight["artifact_digest"],
        "preflight_artifact_id": preflight["artifact_id"],
        "preflight_attempt": preflight["run_attempt"],
        "preflight_run_id": preflight["run_id"],
        "sync_attempt": sync_attempt,
        "sync_base_commit": payload["sync_base_commit"],
        "sync_head_commit": payload["sync_head_commit"],
        "sync_head_tree": payload["sync_head_tree"],
        "sync_repair": sync_repair,
        "sync_repair_attempt": repair_attempt,
        "sync_repair_readiness": repair_readiness,
        "sync_repair_run_id": repair_run_id,
        "sync_run_id": sync_run_id,
    }


def _verify_source_run(
    client: GitHubClient,
    config: SyncConfig,
    number: int,
    run_id: int,
    attempt: int,
    *,
    head_commit: str | None = None,
) -> None:
    expected_head = config.release_commit if head_commit is None else head_commit
    run = client.get(_path(config.repository, f"actions/runs/{run_id}"))
    if (
        not isinstance(run, dict)
        or run.get("id") != run_id
        or run.get("run_attempt") != attempt
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("event") != "pull_request"
        or run.get("head_sha") != expected_head
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
        client, _path(config.repository, f"commits/{expected_head}/pulls")
    )
    matches = [
        pr for pr in pulls
        if isinstance(pr, dict) and pr.get("number") == number
    ]
    if len(matches) != 1:
        raise ApprovalError("Source CI release lacks one exact merged PR association")
    merged_pr = _validate_pr(
        matches[0], config, number, state="closed", expected_head=expected_head
    )
    _timestamp("Source CI associated PR merge timestamp", merged_pr.get("merged_at"))


def _verify_newest_source_run(
    client: GitHubClient,
    config: SyncConfig,
    number: int,
    run_id: int,
    attempt: int,
    *,
    head_commit: str | None = None,
) -> None:
    """Reject a superseded sync-PR run even when its head SHA is unchanged."""

    expected_head = config.release_commit if head_commit is None else head_commit
    payload = client.get(
        _path(config.repository, "actions/workflows/source-ci.yml/runs"),
        {
            "event": "pull_request",
            "head_sha": expected_head,
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
            or item.get("head_sha") != expected_head
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


def _verify_native_sync_validation(
    client: GitHubClient,
    config: SyncConfig,
    contract: Mapping[str, Any],
    *,
    number: int,
    head_commit: str,
    run_id: int,
    run_attempt: int,
    pull_request_state: str,
) -> dict[str, Any]:
    """Verify the genuine required aggregate for the updated PR head."""

    _verify_source_run(
        client,
        config,
        number,
        run_id,
        run_attempt,
        head_commit=head_commit,
    )
    _verify_newest_source_run(
        client,
        config,
        number,
        run_id,
        run_attempt,
        head_commit=head_commit,
    )
    payload = client.get(
        _path(config.repository, f"actions/runs/{run_id}/jobs"),
        {"filter": "latest", "per_page": "100"},
    )
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    total = payload.get("total_count") if isinstance(payload, dict) else None
    if (
        not isinstance(jobs, list)
        or type(total) is not int
        or total != len(jobs)
        or total > 100
    ):
        raise ApprovalError("Updated sync PR Validate PR job list is malformed")
    expected_name = _required_check_contract(contract)["context"]
    matches = [
        item
        for item in jobs
        if isinstance(item, dict) and item.get("name") == expected_name
    ]
    if len(matches) != 1:
        raise ApprovalError("Updated sync PR required aggregate is unavailable")
    job = matches[0]
    job_id = _safe_int("Updated sync PR required job ID", job.get("id"))
    expected_url = (
        f"{config.server_url}/{config.repository}/actions/runs/{run_id}/job/{job_id}"
    )
    if (
        job.get("run_id") != run_id
        or job.get("run_attempt") != run_attempt
        or job.get("head_sha") != head_commit
        or job.get("status") != "completed"
        or job.get("conclusion") != "success"
        or job.get("workflow_name") != "Validate PR"
        or job.get("html_url") != expected_url
    ):
        raise ApprovalError("Updated sync PR required aggregate did not pass")

    required = _required_check_contract(contract)
    check = client.get(_path(config.repository, f"check-runs/{job_id}"))
    app_id, app_slug = _required_check_app(
        check.get("app") if isinstance(check, dict) else None
    )
    suite = check.get("check_suite") if isinstance(check, dict) else None
    associations = check.get("pull_requests") if isinstance(check, dict) else None
    if (
        not isinstance(check, dict)
        or check.get("id") != job_id
        or check.get("name") != expected_name
        or check.get("head_sha") != head_commit
        or check.get("status") != "completed"
        or check.get("conclusion") != "success"
        or check.get("details_url") != expected_url
        or app_id != required["app_id"]
        or app_slug != required["app_slug"]
        or not isinstance(suite, dict)
        or pull_request_state == "OPEN"
        and not _required_check_pull_matches(
            associations, number=number, release_commit=head_commit
        )
        or pull_request_state == "MERGED"
        and associations != []
        and not _required_check_pull_matches(
            associations, number=number, release_commit=head_commit
        )
    ):
        raise ApprovalError("Updated sync PR required check identity is invalid")
    locator = _required_check_locator(check)
    _verify_required_check_protection(client, config, contract)
    _verify_exact_required_check_graphql(
        client,
        config,
        contract,
        check,
        pull_request_state=pull_request_state,
        head_commit=head_commit,
    )
    return {
        "app_id": required["app_id"],
        "app_slug": required["app_slug"],
        "check_run_id": locator["check_run_id"],
        "check_run_node_id": locator["check_run_node_id"],
        "check_suite_id": locator["check_suite_id"],
        "completed_at": check["completed_at"],
        "context": expected_name,
        "details_url": expected_url,
        "head_sha": head_commit,
        "run_attempt": run_attempt,
        "run_id": run_id,
    }


def _verify_merge_commit(
    client: GitHubClient,
    config: SyncConfig,
    merge_commit: str,
    *,
    first_parent: str | None = None,
    second_parent: str | None = None,
) -> dict[str, Any]:
    expected_first_parent = config.source_sha if first_parent is None else first_parent
    expected_second_parent = (
        config.release_commit if second_parent is None else second_parent
    )
    merge = _read_commit(client, config, merge_commit)
    parents = merge.get("parents")
    if (
        not isinstance(parents, list)
        or len(parents) != 2
        or not all(isinstance(item, dict) for item in parents)
        or parents[0].get("sha") != expected_first_parent
        or parents[1].get("sha") != expected_second_parent
    ):
        raise ApprovalError(
            "Synchronization PR was not merged with a merge commit directly "
            "onto its tested source"
        )
    if _read_ref(client, config, "main") != merge_commit:
        raise ApprovalError("Synchronization merge is no longer the current main tip")
    return merge


def _authorize_current_recovery(
    event: Mapping[str, Any],
    *,
    owner: str,
    repository: str,
    version: str,
    number: int,
    sync_head_commit: str,
    actor: str,
    triggering_actor: str,
    run_attempt: int,
    api_url: str,
    server_url: str,
    output: Path,
    token: str,
    work_actor: str,
    transport,
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    """Authorize an updated sync head while selecting only immutable v0.6.2."""

    contract = validation_recovery.load_contract()
    release = contract["release"]
    sync = contract["sync"]
    if (
        version != contract["platform_version"]
        or repository != contract["repository"]
        or number != sync["pull_request"]
    ):
        raise ApprovalError("Updated recovery event is outside the reviewed contract")
    config = _config(
        owner=owner,
        repository=repository,
        version=version,
        source_commit=release["source_commit"],
        release_commit=release["commit"],
        api_url=api_url,
        server_url=server_url,
        output=output,
    )
    client = GitHubClient(api_url, token, transport=transport, sleeper=sleeper)
    pr = event["pull_request"]
    _validate_pr(
        pr,
        config,
        number,
        state="closed",
        expected_head=sync_head_commit,
    )
    merged_at = _timestamp("PR merge timestamp", pr.get("merged_at"))
    merge_commit = pr.get("merge_commit_sha")
    if not isinstance(merge_commit, str) or COMMIT_RE.fullmatch(merge_commit) is None:
        raise ApprovalError("Synchronization merge commit is invalid")

    comments = _comments(client, config, number)
    historical_comment, historical_payload, historical_evidence = (
        _historical_recovery_comment(comments, config, contract)
    )
    current_matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for comment in comments:
        if _marker_payload(comment.get("body"), READY_PREFIX) is not None:
            raise ApprovalError("Ordinary readiness cannot authorize recovery")
        payload = _marker_payload(comment.get("body"), CURRENT_RECOVERY_READY_PREFIX)
        if payload is not None:
            current_matches.append((comment, payload))
    if len(current_matches) != 1:
        raise ApprovalError("Exactly one current recovery readiness marker is required")
    ready_comment, ready_payload = current_matches[0]
    ready_comment_id = _safe_int("Current readiness comment ID", ready_comment.get("id"))
    if _login(ready_comment.get("user")) != BOT_LOGIN:
        raise ApprovalError("Current readiness marker was not created by GitHub Actions")
    ready_at = _timestamp("Current readiness timestamp", ready_comment.get("created_at"))
    historical_at = _timestamp(
        "Historical readiness timestamp", historical_comment.get("created_at")
    )
    if ready_at <= historical_at or ready_at > merged_at:
        raise ApprovalError("Synchronization PR was not merged after current readiness")
    evidence = _validate_current_recovery_ready_payload(ready_payload, config, number)
    if (
        evidence["sync_head_commit"] != sync_head_commit
        or pr.get("base", {}).get("sha") != evidence["sync_base_commit"]
    ):
        raise ApprovalError("Merged synchronization PR differs from current readiness")

    _verify_release(client, config, sync=False)
    repair = evidence["sync_repair"]
    _verify_sync_repair_remote(client, config, repair, work_actor=work_actor)
    update = {
        "base_commit": evidence["sync_base_commit"],
        "initial_head_commit": sync["initial_head_commit"],
        "pull_request": number,
        "release_commit": release["commit"],
        "sync_branch": sync["branch"],
        "sync_head_commit": sync_head_commit,
        "sync_head_tree": evidence["sync_head_tree"],
        "sync_repair": repair,
    }
    _verify_sync_update_remote(client, config, contract, update)
    merge = _verify_merge_commit(
        client,
        config,
        merge_commit,
        first_parent=evidence["sync_base_commit"],
        second_parent=sync_head_commit,
    )
    if _commit_tree(merge, context="Synchronization PR merge") != evidence[
        "sync_head_tree"
    ]:
        raise ApprovalError("Synchronization PR merge tree differs from its tested head")
    commit_data = merge.get("commit")
    commit_message = commit_data.get("message") if isinstance(commit_data, dict) else None
    approved = _marker_payload(commit_message, CURRENT_RECOVERY_APPROVAL_PREFIX)
    expected_approval = approval_payload(ready_payload)
    if approved is None or set(approved) != set(expected_approval) or approved != expected_approval:
        raise ApprovalError("ChatGPT recovery approval does not match current readiness")

    _verify_main_run(
        client, config, evidence["main_run_id"], evidence["main_attempt"]
    )
    _verify_main_run_for_commit(
        client,
        config,
        evidence["sync_base_commit"],
        evidence["sync_repair_run_id"],
        evidence["sync_repair_attempt"],
    )
    _verify_historical_recovery_remote(
        client,
        config,
        contract,
        historical_evidence,
        work_actor=work_actor,
    )
    native = _verify_native_sync_validation(
        client,
        config,
        contract,
        number=number,
        head_commit=sync_head_commit,
        run_id=evidence["sync_run_id"],
        run_attempt=evidence["sync_attempt"],
        pull_request_state="MERGED",
    )
    if native != evidence["native_required_check"]:
        raise ApprovalError("Current native required-check evidence changed")

    # Close both queue-time races before returning a deployable identity.
    _verify_release(client, config, sync=False)
    _verify_sync_update_remote(client, config, contract, update)
    if _read_ref(client, config, "main") != merge_commit:
        raise ApprovalError("Synchronization merge is no longer current main")
    final_comments = _comments(client, config, number)
    final_historical, final_historical_payload, _ = _historical_recovery_comment(
        final_comments, config, contract
    )
    final_current = [
        (comment, payload)
        for comment in final_comments
        if (
            payload := _marker_payload(
                comment.get("body"), CURRENT_RECOVERY_READY_PREFIX
            )
        )
        is not None
    ]
    if (
        final_historical != historical_comment
        or final_historical_payload != historical_payload
        or len(final_current) != 1
        or _safe_int("Current readiness comment ID", final_current[0][0].get("id"))
        != ready_comment_id
        or final_current[0][1] != ready_payload
    ):
        raise ApprovalError("Live recovery readiness changed during authorization")

    report = {
        "approval_record": "merge-commit",
        "approval_nonce": ready_payload["approval_nonce"],
        "feature_readiness": evidence["feature_readiness"],
        "feature_pr_number": evidence["feature_readiness"]["feature_pr"]["number"],
        "feature_head_sha": evidence["feature_readiness"]["feature_pr"]["head_sha"],
        "main_validation_run_attempt": evidence["main_attempt"],
        "main_validation_run_id": evidence["main_run_id"],
        "manifest_sha256": release["manifest_sha256"],
        "merge_commit": merge_commit,
        "platform_version": version,
        "preflight_artifact": contract["preflight"]["artifact_name"],
        "preflight_artifact_digest": evidence["preflight_artifact_digest"],
        "preflight_artifact_id": evidence["preflight_artifact_id"],
        "preflight_run_attempt": evidence["preflight_attempt"],
        "preflight_run_id": evidence["preflight_run_id"],
        "pull_request": number,
        "recovery_validation": ready_payload["recovery_validation"],
        "release_commit": release["commit"],
        "repository": repository,
        "schema_version": SCHEMA_VERSION,
        "source_commit": release["source_commit"],
        "sync_base_commit": evidence["sync_base_commit"],
        "sync_head_commit": sync_head_commit,
        "sync_head_tree": evidence["sync_head_tree"],
        "sync_validation_run_attempt": evidence["sync_attempt"],
        "sync_validation_run_id": evidence["sync_run_id"],
        "validation_mode": "published-release-sync-update",
    }
    return report


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
    if version == "0.6.2" and repository == "Fifty5D/B-UH-AllianceAuth":
        recovery_contract = validation_recovery.load_contract()
        if release_commit != recovery_contract["release"]["commit"]:
            return _authorize_current_recovery(
                event,
                owner=owner,
                repository=repository,
                version=version,
                number=number,
                sync_head_commit=release_commit,
                actor=actor,
                triggering_actor=triggering_actor,
                run_attempt=run_attempt,
                api_url=api_url,
                server_url=server_url,
                output=output,
                token=token,
                work_actor=work_actor,
                transport=transport,
                sleeper=sleeper,
            )
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
        evidence["publication_commit"] if recovered else config.source_sha
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
        continuation_pull = _verify_continuation_remote(
            client,
            config,
            evidence["attestation"],
            state="closed",
            work_actor=work_actor,
        )
        continuation_merged_at = _timestamp(
            "Recovery continuation merge timestamp",
            continuation_pull.get("merged_at"),
        )
        repair_pull = _verify_repair_remote(
            client,
            config,
            evidence["attestation"],
            state="closed",
            work_actor=work_actor,
        )
        repair_merged_at = _timestamp(
            "Recovery digest repair merge timestamp", repair_pull.get("merged_at")
        )
        publication_pull = _verify_publication_repair_remote(
            client,
            config,
            evidence["publication_repair"],
            state="closed",
            work_actor=work_actor,
        )
        publication_merged_at = _timestamp(
            "Recovery publication repair merge timestamp",
            publication_pull.get("merged_at"),
        )
        if activation_merged_at >= continuation_merged_at:
            raise ApprovalError(
                "Recovery continuation did not merge after its activation"
            )
        if continuation_merged_at >= repair_merged_at:
            raise ApprovalError(
                "Recovery digest repair did not merge after PR #54"
            )
        if repair_merged_at >= publication_merged_at:
            raise ApprovalError(
                "Recovery publication repair did not merge after PR #55"
            )
        if publication_merged_at >= merged_at:
            raise ApprovalError(
                "Synchronization PR did not merge after recovery publication repair"
            )
        _verify_main_run_for_commit(
            client,
            config,
            evidence["activation_commit"],
            evidence["activation_run_id"],
            evidence["activation_attempt"],
        )
        _verify_main_run_for_commit(
            client,
            config,
            evidence["continuation_commit"],
            evidence["continuation_run_id"],
            evidence["continuation_attempt"],
        )
        _verify_main_run_for_commit(
            client,
            config,
            evidence["repair_commit"],
            evidence["repair_run_id"],
            evidence["repair_attempt"],
        )
        _verify_main_run_for_commit(
            client,
            config,
            evidence["publication_commit"],
            evidence["publication_run_id"],
            evidence["publication_attempt"],
        )
        _verify_recovery_preflight(
            client, config, contract, work_actor=work_actor
        )
        _verify_failed_recovery_validation(
            client, config, contract, work_actor=work_actor
        )
        _verify_failed_recovery_publication(
            client, config, contract, work_actor=work_actor
        )
        recovery_artifact, recovery_lanes, recovered_check = (
            _verify_partial_recovery_publication(
                client,
                config,
                contract,
                evidence["attestation"],
                pull_request_state="MERGED",
                work_actor=work_actor,
            )
        )
        if (
            recovery_artifact["id"] != evidence["artifact_id"]
            or recovery_artifact["digest"] != evidence["artifact_digest"]
            or recovery_lanes != evidence["required_lanes"]
            or recovered_check != evidence["required_check"]
        ):
            raise ApprovalError("Recovery validation lane evidence changed")
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
        _verify_failed_recovery_validation(
            client, config, contract, work_actor=work_actor
        )
        _verify_failed_recovery_publication(
            client, config, contract, work_actor=work_actor
        )
        _verify_main_run_for_commit(
            client,
            config,
            evidence["activation_commit"],
            evidence["activation_run_id"],
            evidence["activation_attempt"],
        )
        _verify_main_run_for_commit(
            client,
            config,
            evidence["continuation_commit"],
            evidence["continuation_run_id"],
            evidence["continuation_attempt"],
        )
        _verify_main_run_for_commit(
            client,
            config,
            evidence["repair_commit"],
            evidence["repair_run_id"],
            evidence["repair_attempt"],
        )
        _verify_publication_repair_remote(
            client,
            config,
            evidence["publication_repair"],
            state="closed",
            work_actor=work_actor,
        )
        _verify_main_run_for_commit(
            client,
            config,
            evidence["publication_commit"],
            evidence["publication_run_id"],
            evidence["publication_attempt"],
        )
        final_recovery_artifact, recovery_lanes, recovered_check = (
            _verify_partial_recovery_publication(
                client,
                config,
                contract,
                evidence["attestation"],
                pull_request_state="MERGED",
                work_actor=work_actor,
            )
        )
        if (
            final_recovery_artifact["id"] != recovery_artifact["id"]
            or final_recovery_artifact["digest"] != recovery_artifact["digest"]
            or recovery_lanes != evidence["required_lanes"]
            or recovered_check != evidence["required_check"]
        ):
            raise ApprovalError("Partial recovery publication changed during authorization")
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
            "sync_base_commit",
            "sync_head_commit",
            "sync_head_tree",
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
    recovery_update_parser = subparsers.add_parser("recover-ready-update")
    authorize_parser = subparsers.add_parser("authorize")
    verify_parser = subparsers.add_parser("verify-artifact")
    for subparser in (
        ready_parser,
        recovery_parser,
        recovery_update_parser,
        authorize_parser,
    ):
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
    recovery_parser.add_argument("--activation-readiness", type=Path, required=True)
    recovery_parser.add_argument("--continuation-readiness", type=Path, required=True)
    recovery_parser.add_argument("--repair-readiness", type=Path, required=True)
    recovery_parser.add_argument("--publication-repair", type=Path, required=True)
    recovery_parser.add_argument("--publication-readiness", type=Path, required=True)
    recovery_parser.add_argument(
        "--validation-attestation", type=Path, required=True
    )
    recovery_parser.add_argument("--validation-artifact-id", type=int, required=True)
    recovery_parser.add_argument("--validation-artifact-name", required=True)
    recovery_parser.add_argument("--validation-artifact-digest", required=True)
    recovery_parser.add_argument("--work-actor", default="")
    recovery_update_parser.add_argument(
        "--recovery-contract", type=Path, required=True
    )
    recovery_update_parser.add_argument("--sync-update", type=Path, required=True)
    recovery_update_parser.add_argument(
        "--sync-repair-readiness", type=Path, required=True
    )
    recovery_update_parser.add_argument("--work-actor", default="")
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
            activation_readiness_raw = args.activation_readiness.read_text(
                encoding="ascii"
            )
            activation_readiness = json.loads(activation_readiness_raw)
            if activation_readiness_raw != _canonical(activation_readiness) + "\n":
                raise ApprovalError("Activation readiness evidence is not canonical")
            continuation_readiness_raw = args.continuation_readiness.read_text(
                encoding="ascii"
            )
            continuation_readiness = json.loads(continuation_readiness_raw)
            if continuation_readiness_raw != _canonical(continuation_readiness) + "\n":
                raise ApprovalError("Continuation readiness evidence is not canonical")
            repair_readiness_raw = args.repair_readiness.read_text(encoding="ascii")
            repair_readiness = json.loads(repair_readiness_raw)
            if repair_readiness_raw != _canonical(repair_readiness) + "\n":
                raise ApprovalError("Digest repair readiness evidence is not canonical")
            publication_repair_raw = args.publication_repair.read_text(
                encoding="ascii"
            )
            publication_repair_report = json.loads(publication_repair_raw)
            if (
                publication_repair_raw
                != _canonical(publication_repair_report) + "\n"
                or not isinstance(publication_repair_report, dict)
                or set(publication_repair_report)
                != {
                    "publication_repair",
                    "recovery_id",
                    "repair",
                    "schema_version",
                }
            ):
                raise ApprovalError("Publication repair evidence is not canonical")
            publication_readiness_raw = args.publication_readiness.read_text(
                encoding="ascii"
            )
            publication_readiness = json.loads(publication_readiness_raw)
            if publication_readiness_raw != _canonical(publication_readiness) + "\n":
                raise ApprovalError("Publication repair readiness is not canonical")
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
                activation_readiness=activation_readiness,
                continuation_readiness=continuation_readiness,
                repair_readiness=repair_readiness,
                publication_repair=publication_repair_report[
                    "publication_repair"
                ],
                publication_readiness=publication_readiness,
                validation_attestation=validation_attestation,
                validation_artifact_id=args.validation_artifact_id,
                validation_artifact_name=args.validation_artifact_name,
                validation_artifact_digest=args.validation_artifact_digest,
                work_actor=args.work_actor,
            )
            output_values = report["ready"]
        elif args.command == "recover-ready-update":
            contract = validation_recovery.load_contract(args.recovery_contract)
            sync_update_raw = args.sync_update.read_text(encoding="ascii")
            sync_update_report = json.loads(sync_update_raw)
            if (
                sync_update_raw != _canonical(sync_update_report) + "\n"
                or not isinstance(sync_update_report, dict)
                or set(sync_update_report)
                != {"recovery_id", "schema_version", "sync_update"}
                or sync_update_report.get("recovery_id") != contract["recovery_id"]
                or sync_update_report.get("schema_version")
                != validation_recovery.SCHEMA_VERSION
            ):
                raise ApprovalError("Synchronization update evidence is not canonical")
            repair_readiness_raw = args.sync_repair_readiness.read_text(
                encoding="ascii"
            )
            repair_readiness = json.loads(repair_readiness_raw)
            if repair_readiness_raw != _canonical(repair_readiness) + "\n":
                raise ApprovalError("Sync-head repair readiness is not canonical")
            report = ready_recovery_update(
                contract,
                token,
                owner=args.owner,
                repository=args.repository,
                api_url=args.api_url,
                server_url=args.server_url,
                output=args.output,
                sync_update=sync_update_report["sync_update"],
                sync_repair_readiness=repair_readiness,
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
