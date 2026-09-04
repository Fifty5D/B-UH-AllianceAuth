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


SCHEMA_VERSION = 1
READY_PREFIX = "<!-- buh-platform-ready:v1 "
APPROVAL_PREFIX = "<!-- buh-chatgpt-approved:v1 "
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
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


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
        or _workflow_path(run) != ".github/workflows/auto-platform-release.yml"
        or _repo_name(run.get("repository")) != config.repository
        or _repo_name(run.get("head_repository")) != config.repository
        or _login(run.get("actor")) != config.owner
    ):
        raise ApprovalError("Automatic release workflow identity is invalid")
    if completed:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise ApprovalError("Automatic release workflow did not complete successfully")
    elif run.get("status") not in {"in_progress", "queued"}:
        raise ApprovalError("Automatic release workflow is not active")
    return run


def _artifact(
    client: GitHubClient,
    config: SyncConfig,
    run_id: int,
    name: str,
) -> dict[str, Any]:
    payload = client.get(
        _path(config.repository, f"actions/runs/{run_id}/artifacts"),
        {"name": name, "per_page": "100"},
    )
    artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
    exact = [
        item
        for item in artifacts or []
        if isinstance(item, dict)
        and item.get("name") == name
        and item.get("expired") is False
        and type(item.get("id")) is int
        and item["id"] > 0
        and type(item.get("size_in_bytes")) is int
        and 0 < item["size_in_bytes"] <= 40 * 1024 * 1024
    ]
    if not isinstance(artifacts, list) or len(exact) != 1:
        raise ApprovalError("Exact retained preflight artifact is unavailable")
    return exact[0]


def _nonce(fields: Mapping[str, Any]) -> str:
    bound = {
        key: fields[key]
        for key in (
            "manifest_sha256",
            "platform_version",
            "preflight_artifact",
            "preflight_run_attempt",
            "preflight_run_id",
            "pull_request",
            "release_commit",
            "repository",
            "source_ci_run_attempt",
            "source_ci_run_id",
            "source_commit",
        )
    }
    return hashlib.sha256((_canonical(bound) + "\n").encode("ascii")).hexdigest()


def approval_payload(ready: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "approval_nonce": ready["approval_nonce"],
        "platform_version": ready["platform_version"],
        "release_commit": ready["release_commit"],
        "schema_version": SCHEMA_VERSION,
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
) -> dict[str, Any]:
    approval = marker(APPROVAL_PREFIX, approval_payload(ready))
    body = (
        f"{marker(READY_PREFIX, ready)}\n\n"
        "### Ready for the one production approval in ChatGPT\n\n"
        "Source CI and the no-change production preflight passed for this exact "
        "immutable release. Production has not been changed.\n\n"
        "After the repository owner explicitly approves this release in ChatGPT, "
        "ChatGPT must place this exact record in the merge commit message and "
        "perform one merge-commit action:\n\n"
        f"```html\n{approval}\n```"
    )
    response = client.post(
        _path(config.repository, f"issues/{number}/comments"), {"body": body}
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
    _get_pr(client, config, number, "open")
    _verify_release(client, config, sync=True)
    _workflow_run(
        client,
        config,
        preflight_run_id,
        preflight_run_attempt,
        completed=False,
    )
    _artifact(client, config, preflight_run_id, preflight_artifact)
    source_run = _source_ci_run(
        client, config, number, sleeper=sleeper, polls=polls
    )
    existing = [
        item
        for item in _comments(client, config, number)
        if _marker_payload(item.get("body"), READY_PREFIX) is not None
    ]
    if existing:
        raise ApprovalError("A readiness marker already exists for this pull request")
    fields: dict[str, Any] = {
        "manifest_sha256": manifest_sha256,
        "platform_version": config.version,
        "preflight_artifact": preflight_artifact,
        "preflight_run_attempt": preflight_run_attempt,
        "preflight_run_id": preflight_run_id,
        "pull_request": number,
        "release_commit": config.release_commit,
        "repository": config.repository,
        "schema_version": SCHEMA_VERSION,
        "source_ci_run_attempt": source_run["run_attempt"],
        "source_ci_run_id": source_run["id"],
        "source_commit": config.source_sha,
    }
    fields["approval_nonce"] = _nonce(fields)
    comment = _post_ready_comment(client, config, number, fields)
    return {
        "approval_marker": marker(APPROVAL_PREFIX, approval_payload(fields)),
        "comment_id": comment["id"],
        "ready": fields,
        "schema_version": SCHEMA_VERSION,
    }


READY_KEYS = {
    "approval_nonce",
    "manifest_sha256",
    "platform_version",
    "preflight_artifact",
    "preflight_run_attempt",
    "preflight_run_id",
    "pull_request",
    "release_commit",
    "repository",
    "schema_version",
    "source_ci_run_attempt",
    "source_ci_run_id",
    "source_commit",
}


def _validate_ready_payload(payload: Mapping[str, Any], config: SyncConfig, number: int):
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
    run_id = _safe_int("Preflight run ID", payload.get("preflight_run_id"))
    run_attempt = _safe_int(
        "Preflight run attempt", payload.get("preflight_run_attempt"), maximum=10**6
    )
    source_run_id = _safe_int("Source CI run ID", payload.get("source_ci_run_id"))
    source_attempt = _safe_int(
        "Source CI run attempt", payload.get("source_ci_run_attempt"), maximum=10**6
    )
    if payload.get("preflight_artifact") != f"platform-v2-preflight-{run_id}-{run_attempt}":
        raise ApprovalError("Readiness marker artifact identity is invalid")
    if payload["approval_nonce"] != _nonce(payload):
        raise ApprovalError("Readiness marker nonce is invalid")
    return run_id, run_attempt, source_run_id, source_attempt


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
        or number not in _run_pr_numbers(run)
    ):
        raise ApprovalError("Recorded sync PR Source CI evidence is invalid")


def _verify_merge_commit(
    client: GitHubClient,
    config: SyncConfig,
    merge_commit: str,
) -> dict[str, Any]:
    merge = _read_commit(client, config, merge_commit)
    parents = merge.get("parents")
    if (
        not isinstance(parents, list)
        or len(parents) != 2
        or not all(isinstance(item, dict) for item in parents)
        or parents[1].get("sha") != config.release_commit
    ):
        raise ApprovalError("Synchronization PR was not merged with a merge commit")
    comparison = client.get(
        _path(config.repository, f"compare/{merge_commit}...main")
    )
    if (
        not isinstance(comparison, dict)
        or comparison.get("status") not in {"identical", "ahead"}
        or not isinstance(comparison.get("merge_base_commit"), dict)
        or comparison["merge_base_commit"].get("sha") != merge_commit
    ):
        raise ApprovalError("Synchronization merge is not an ancestor of current main")
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
    merge = _verify_merge_commit(client, config, merge_commit)

    comments = _comments(client, config, number)
    ready_comments = []
    for comment in comments:
        ready_payload = _marker_payload(comment.get("body"), READY_PREFIX)
        if ready_payload is not None:
            ready_comments.append((comment, ready_payload))
    if len(ready_comments) != 1:
        raise ApprovalError(
            "Exactly one GitHub Actions readiness marker is required"
        )
    ready_comment, ready_payload = ready_comments[0]
    if _login(ready_comment.get("user")) != BOT_LOGIN:
        raise ApprovalError("Readiness marker was not created by GitHub Actions")
    ready_at = _timestamp("Readiness comment timestamp", ready_comment.get("created_at"))
    if ready_at > merged_at:
        raise ApprovalError("Synchronization PR was merged before readiness")
    run_id, preflight_attempt, source_run_id, source_attempt = _validate_ready_payload(
        ready_payload, config, number
    )
    commit_data = merge.get("commit")
    commit_message = (
        commit_data.get("message") if isinstance(commit_data, dict) else None
    )
    approved = _marker_payload(commit_message, APPROVAL_PREFIX)
    if approved is None:
        raise ApprovalError("Merge commit lacks the ChatGPT approval marker")
    if set(approved) != {
        "approval_nonce",
        "platform_version",
        "release_commit",
        "schema_version",
    } or approved != approval_payload(ready_payload):
        raise ApprovalError("ChatGPT approval marker does not match readiness evidence")
    _workflow_run(client, config, run_id, preflight_attempt, completed=True)
    _verify_source_run(
        client, config, number, source_run_id, source_attempt
    )
    artifact = _artifact(
        client, config, run_id, ready_payload["preflight_artifact"]
    )
    return {
        "approval_record": "merge-commit",
        "approval_nonce": ready_payload["approval_nonce"],
        "manifest_sha256": ready_payload["manifest_sha256"],
        "merge_commit": merge_commit,
        "platform_version": version,
        "preflight_artifact": ready_payload["preflight_artifact"],
        "preflight_artifact_id": artifact["id"],
        "preflight_run_attempt": preflight_attempt,
        "preflight_run_id": run_id,
        "pull_request": number,
        "release_commit": release_commit,
        "repository": repository,
        "schema_version": SCHEMA_VERSION,
        "source_commit": source_commit,
    }


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
    if receiver != {
        "manifest_sha256": expected["manifest_sha256"],
        "platform_version": expected["platform_version"],
        "result": "preflight-passed",
        "schema_version": SCHEMA_VERSION,
    }:
        raise ApprovalError("Receiver evidence does not prove the exact preflight")
    if (
        lines[0] != "B-UH Platform v2 guarded attempt report"
        or not isinstance(attempt_report, dict)
        or attempt_report.get("schema_version") != SCHEMA_VERSION
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
            "manifest_sha256",
            "platform_version",
            "preflight_artifact",
            "preflight_run_attempt",
            "preflight_run_id",
            "pull_request",
            "release_commit",
            "source_commit",
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
    authorize_parser = subparsers.add_parser("authorize")
    verify_parser = subparsers.add_parser("verify-artifact")
    for subparser in (ready_parser, authorize_parser):
        subparser.add_argument("--owner", required=True)
        subparser.add_argument("--repository", required=True)
        subparser.add_argument("--api-url", required=True)
        subparser.add_argument("--server-url", required=True)
        subparser.add_argument("--output", type=Path, required=True)
    ready_parser.add_argument("--version", required=True)
    ready_parser.add_argument("--source-commit", required=True)
    ready_parser.add_argument("--release-commit", required=True)
    ready_parser.add_argument("--pull-request", type=int, required=True)
    ready_parser.add_argument("--manifest-sha256", required=True)
    ready_parser.add_argument("--preflight-run-id", type=int, required=True)
    ready_parser.add_argument("--preflight-run-attempt", type=int, required=True)
    ready_parser.add_argument("--preflight-artifact", required=True)
    authorize_parser.add_argument("--actor", required=True)
    authorize_parser.add_argument("--triggering-actor", required=True)
    authorize_parser.add_argument("--run-attempt", type=int, required=True)
    authorize_parser.add_argument("--event", type=Path, required=True)
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
            stdout.write('{"result":"preflight-evidence-verified","schema_version":1}\n')
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
            report = ready(
                config,
                token,
                number=args.pull_request,
                manifest_sha256=args.manifest_sha256,
                preflight_run_id=args.preflight_run_id,
                preflight_run_attempt=args.preflight_run_attempt,
                preflight_artifact=args.preflight_artifact,
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
            )
            output_values = report
        _write_json(args.output, report)
        _append_outputs(environment.get("GITHUB_OUTPUT"), output_values)
        stdout.write(_canonical(report) + "\n")
        return 0
    except (ApprovalError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        stderr.write(f"platform approval error: {str(exc).splitlines()[0][:500]}\n")
        return 2
    except Exception:
        stderr.write("platform approval error: unexpected internal failure\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
