#!/usr/bin/env python3
"""Bind one owner deployment dispatch to one fresh immutable-release preflight."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

import platform_approval as approval


WORKFLOW = ".github/workflows/deploy-platform-v2.yml"
SCHEMA_VERSION = approval.SCHEMA_VERSION


def _run_title(mode: str, version: str, preflight_run_id: str, attempt: str) -> str:
    return f"Platform v2 {mode} / v{version} / preflight {preflight_run_id}-{attempt}"


def _workflow_runs(client: approval.GitHubClient, repository: str) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    total: int | None = None
    for page in range(1, approval.MAX_PAGES + 1):
        payload = client.get(
            approval._path(repository, "actions/workflows/deploy-platform-v2.yml/runs"),
            {
                "branch": "main",
                "event": "workflow_dispatch",
                "per_page": "100",
                "page": str(page),
            },
        )
        runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
        count = payload.get("total_count") if isinstance(payload, dict) else None
        if not isinstance(runs, list) or type(count) is not int or count < 0:
            raise approval.ApprovalError("Manual deployment run list is malformed")
        if total is None:
            total = count
        elif total != count:
            raise approval.ApprovalError("Manual deployment run list changed during authorization")
        if any(not isinstance(run, dict) for run in runs):
            raise approval.ApprovalError("Manual deployment run list is malformed")
        selected.extend(runs)
        if len(runs) < 100:
            if len(selected) != total:
                raise approval.ApprovalError("Manual deployment run list is incomplete")
            return selected
    raise approval.ApprovalError("Manual deployment run list exceeded its fixed bound")


def authorize(
    *,
    owner: str,
    repository: str,
    actor: str,
    triggering_actor: str,
    run_id: int,
    run_attempt: int,
    workflow_commit: str,
    version: str,
    source_commit: str,
    release_commit: str,
    manifest_sha256: str,
    preflight_run_id: int,
    preflight_run_attempt: int,
    preflight_artifact_id: int,
    preflight_artifact_digest: str,
    api_url: str,
    server_url: str,
    output: Path,
    token: str,
    client: approval.GitHubClient | None = None,
) -> dict[str, Any]:
    """Reauthorize one first-attempt owner run and its unconsumed preflight."""

    if (
        actor != owner
        or triggering_actor != owner
        or run_attempt != 1
        or preflight_run_attempt != 1
        or approval.COMMIT_RE.fullmatch(workflow_commit) is None
        or approval.NONCE_RE.fullmatch(manifest_sha256) is None
        or approval.DIGEST_RE.fullmatch(preflight_artifact_digest) is None
    ):
        raise approval.ApprovalError("Reviewed manual deployment identity is invalid")
    run_id = approval._safe_int("Deployment run ID", run_id)
    preflight_run_id = approval._safe_int("Preflight run ID", preflight_run_id)
    preflight_artifact_id = approval._safe_int(
        "Preflight artifact ID", preflight_artifact_id
    )
    if run_id == preflight_run_id:
        raise approval.ApprovalError("Deployment and preflight runs must be distinct")

    config = approval._config(
        owner=owner,
        repository=repository,
        version=version,
        source_commit=source_commit,
        release_commit=release_commit,
        api_url=api_url,
        server_url=server_url,
        output=output,
    )
    api = client or approval.GitHubClient(api_url, token)
    if approval._read_ref(api, config, "main") != workflow_commit:
        raise approval.ApprovalError("Manual deployment is not running from current main")
    approval._verify_release(api, config, sync=False)
    main_run = approval._main_ci_run(api, config, workflow_commit)

    preflight = api.get(
        approval._path(repository, f"actions/runs/{preflight_run_id}")
    )
    if (
        not isinstance(preflight, dict)
        or preflight.get("id") != preflight_run_id
        or preflight.get("run_attempt") != 1
        or preflight.get("event") != "workflow_dispatch"
        or preflight.get("head_sha") != workflow_commit
        or preflight.get("head_branch") != "main"
        or preflight.get("status") != "completed"
        or preflight.get("conclusion") != "success"
        or preflight.get("display_title")
        != _run_title("preflight", version, "none", "none")
        or approval._workflow_path(preflight) != WORKFLOW
        or approval._repo_name(preflight.get("repository")) != repository
        or approval._repo_name(preflight.get("head_repository")) != repository
        or approval._login(preflight.get("actor")) != owner
        or approval._login(preflight.get("triggering_actor")) != owner
    ):
        raise approval.ApprovalError("Fresh manual preflight run identity is invalid")

    expected_title = _run_title("deploy", version, str(preflight_run_id), "1")
    matching = [
        run
        for run in _workflow_runs(api, repository)
        if run.get("display_title") == expected_title
    ]
    if len(matching) != 1:
        raise approval.ApprovalError(
            "Fresh manual preflight is missing or has already been consumed"
        )
    current = matching[0]
    if (
        current.get("id") != run_id
        or current.get("run_attempt") != 1
        or current.get("event") != "workflow_dispatch"
        or current.get("head_sha") != workflow_commit
        or current.get("head_branch") != "main"
        or current.get("status") not in {"queued", "in_progress"}
        or current.get("conclusion") is not None
        or approval._workflow_path(current) != WORKFLOW
        or approval._repo_name(current.get("repository")) != repository
        or approval._repo_name(current.get("head_repository")) != repository
        or approval._login(current.get("actor")) != owner
        or approval._login(current.get("triggering_actor")) != owner
    ):
        raise approval.ApprovalError("Reviewed manual deployment run identity changed")
    preflight_finished = approval._timestamp(
        "Preflight completion", preflight.get("updated_at")
    )
    deployment_created = approval._timestamp(
        "Deployment creation", current.get("created_at")
    )
    if preflight_finished > deployment_created:
        raise approval.ApprovalError("Deployment approval predates its preflight")

    artifact_name = f"platform-v2-preflight-{preflight_run_id}-1"
    artifact = approval._artifact(
        api,
        config,
        preflight_run_id,
        artifact_name,
        head_sha=workflow_commit,
        expected_id=preflight_artifact_id,
        expected_digest=preflight_artifact_digest,
    )
    return {
        "authorization_mode": "reviewed-manual-deployment",
        "deployment_run_attempt": 1,
        "deployment_run_id": run_id,
        "main_validation_run_attempt": main_run["run_attempt"],
        "main_validation_run_id": main_run["id"],
        "manifest_sha256": manifest_sha256,
        "platform_version": version,
        "preflight_artifact": artifact_name,
        "preflight_artifact_digest": artifact["digest"],
        "preflight_artifact_id": artifact["id"],
        "preflight_run_attempt": 1,
        "preflight_run_id": preflight_run_id,
        "release_commit": release_commit,
        "repository": repository,
        "schema_version": SCHEMA_VERSION,
        "source_commit": source_commit,
        "workflow_commit": workflow_commit,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--triggering-actor", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--workflow-commit", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--release-commit", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--preflight-run-id", type=int, required=True)
    parser.add_argument("--preflight-run-attempt", type=int, required=True)
    parser.add_argument("--preflight-artifact-id", type=int, required=True)
    parser.add_argument("--preflight-artifact-digest", required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
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
        report = authorize(**vars(args), token=environment.get("GITHUB_TOKEN", ""))
        approval._write_json(args.output, report)
        stdout.write(approval._canonical(report) + "\n")
        return 0
    except (approval.ApprovalError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        stderr.write(f"manual deployment approval error: {str(exc).splitlines()[0][:500]}\n")
        return 2
    except Exception:
        stderr.write("manual deployment approval error: unexpected internal failure\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
