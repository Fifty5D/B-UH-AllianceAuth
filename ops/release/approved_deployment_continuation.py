"""One reviewed continuation of an approved merge which never reached the VPS.

This is not a deployment retry or an approval publisher. A fresh, first-attempt
owner dispatch on the sole repair merge reuses the existing exact approval only
after authenticating the failed/skipped history and all live evidence again.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

import platform_approval as approval
import deployment_evidence

REPOSITORY = "Fifty5D/B-UH-AllianceAuth"
SYNC_MERGE = "73569d32dc4f64fc1733cfc00b1d4c48928d5c5c"
SYNC_HEAD = "553bc2a3dcef0029d672495a1e84b1ca78c02306"
SYNC_TREE = "4be002f9bf3b4535bd9d4837fcd0340682d6bfb3"
FAILED_RUN = 34679176355
FAILED_JOB = 103514449958
SKIPPED_JOB = 103514575417
APPROVAL_NONCE = "7a9183fcf9ac5a72e761f48c69567b5ac4462acaea351b97373afe9de4a8ff4b"
BRANCH = "codex/complete-v062-approved-deployment"
WORKFLOW = ".github/workflows/deploy-approved-platform-release.yml"
CONFIRMATION = "CONTINUE APPROVED V0.6.2"
ALLOWED_PATHS = frozenset({
    WORKFLOW,
    ".github/workflows/deploy-platform-v2.yml",
    "changes/approved-v062-deployment-continuation.toml",
    "ops/release/README.md",
    "ops/release/platform_approval.py",
    "ops/release/deployment_evidence.py",
    "ops/release/approved_deployment_continuation.py",
    "ops/release/validation_recovery.py",
    "tests/platform/test_configuration.py",
    "tests/platform/test_release_workflow_execution.py",
    "tests/release/test_deployment_continuation.py",
    "tests/release/test_platform_approval.py",
    "tests/release/fixtures/v062-approved-deployment.json",
})


def validate_repair(root: Path, current: str) -> dict:
    """Keep the release hold across exactly this non-runtime repair merge."""
    recovery = approval.validation_recovery
    parents = recovery._parents(root, current)
    if len(parents) != 2 or parents[0] != SYNC_MERGE:
        raise recovery.ValidationRecoveryError("Main is outside the approved deployment repair")
    paths = set(recovery._changed_paths(root, SYNC_MERGE, current))
    if not paths or not paths <= ALLOWED_PATHS:
        raise recovery.ValidationRecoveryError("Deployment repair changes unreviewed paths")
    if recovery._tree(root, parents[1]) != recovery._tree(root, current):
        raise recovery.ValidationRecoveryError("Deployment repair merge differs from tested head")
    return {"base_commit": SYNC_MERGE, "feature_head": parents[1], "merge_commit": current}


def _failed_attempt(client, config) -> None:
    run = client.get(approval._path(REPOSITORY, f"actions/runs/{FAILED_RUN}"))
    if (
        run.get("id") != FAILED_RUN or run.get("run_attempt") != 1
        # Actions exposes the sync PR head here, not github.sha's merge commit.
        or run.get("head_sha") != SYNC_HEAD or run.get("head_branch") != "sync/platform-v0.6.2"
        or run.get("event") != "pull_request_target"
        or run.get("status") != "completed" or run.get("conclusion") != "failure"
        or approval._workflow_path(run) != WORKFLOW
        or approval._repo_name(run.get("repository")) != REPOSITORY
        or approval._repo_name(run.get("head_repository")) != REPOSITORY
        or approval._login(run.get("actor")) != config.owner
        or approval._login(run.get("triggering_actor")) != config.owner
    ):
        raise approval.ApprovalError("Original approved deployment failure identity changed")
    result = client.get(
        approval._path(REPOSITORY, f"actions/runs/{FAILED_RUN}/jobs"), {"per_page": "100"}
    )
    jobs = result.get("jobs")
    if not isinstance(jobs, list) or result.get("total_count") != 3 or len(jobs) != 3:
        raise approval.ApprovalError("Original approved deployment jobs changed")
    by_id = {job.get("id"): job for job in jobs}
    expected = {
        FAILED_JOB: ("Bind merge to ChatGPT approval and preflight", "failure"),
        SKIPPED_JOB: ("Deploy the approved immutable release", "skipped"),
        103514574740: ("Report deployment result for ChatGPT", "success"),
    }
    for identity, (name, conclusion) in expected.items():
        job = by_id.get(identity, {})
        if (
            job.get("name") != name or job.get("conclusion") != conclusion
            or job.get("status") != "completed" or job.get("head_sha") != SYNC_HEAD
            or job.get("run_id") != FAILED_RUN or job.get("run_attempt") != 1
        ):
            raise approval.ApprovalError("Original deployment did not stop before server access")
    steps = {step.get("name"): step for step in by_id[FAILED_JOB].get("steps", [])}
    for name, conclusion in {
        "Reverify the merge, evidence, and one ChatGPT approval": "success",
        "Verify receiver and observer preflight evidence": "success",
        "Close authorization races before deploy": "failure",
    }.items():
        if steps.get(name, {}).get("conclusion") != conclusion:
            raise approval.ApprovalError("Original authorization step evidence changed")
    if by_id[SKIPPED_JOB].get("steps"):
        raise approval.ApprovalError("Original deployment contains unexpected executed steps")


def authorize(event: Mapping, *, environment: Mapping, **arguments) -> dict:
    owner = arguments["owner"]
    if (
        arguments["repository"] != REPOSITORY or owner != "Fifty5D"
        or arguments["actor"] != owner or arguments["triggering_actor"] != owner
        or arguments["run_attempt"] != 1 or event.get("ref") not in {"main", "refs/heads/main"}
        or environment.get("GITHUB_REF") != "refs/heads/main"
        or approval._login(event.get("sender")) != owner
        or event.get("inputs") != {"confirmation": CONFIRMATION}
        or environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or environment.get("GITHUB_WORKFLOW_REF") != f"{REPOSITORY}/{WORKFLOW}@refs/heads/main"
    ):
        raise approval.ApprovalError("Not the first owner-requested approved v0.6.2 continuation")
    current = environment.get("GITHUB_SHA", "")
    if approval.COMMIT_RE.fullmatch(current) is None:
        raise approval.ApprovalError("Continuation workflow source is invalid")
    run_id = approval._safe_int("Continuation run ID", int(environment.get("GITHUB_RUN_ID", "0")))
    contract = approval.validation_recovery.load_contract()
    release = contract["release"]
    config = approval._config(
        owner=owner, repository=REPOSITORY, version="0.6.2",
        source_commit=release["source_commit"], release_commit=release["commit"],
        api_url=arguments["api_url"], server_url=arguments["server_url"], output=arguments["output"],
    )
    client = approval.GitHubClient(
        arguments["api_url"], arguments["token"],
        **{key: arguments[key] for key in ("transport", "sleeper") if key in arguments},
    )
    if approval._read_ref(client, config, "main") != current:
        raise approval.ApprovalError("Continuation is not current main")
    commit = approval._read_commit(client, config, current)
    parents = commit.get("parents", [])
    if len(parents) != 2 or parents[0].get("sha") != SYNC_MERGE:
        raise approval.ApprovalError("Continuation is not the direct reviewed repair merge")
    associations = approval._pages(client, approval._path(REPOSITORY, f"commits/{current}/pulls"))
    if len(associations) != 1:
        raise approval.ApprovalError("Continuation lacks one exact repair PR")
    identity = {
        "merge_commit": current, "feature_head": parents[1].get("sha"),
        "tree": approval._commit_tree(commit, context="Deployment repair"),
        "pull_request": associations[0].get("number"),
    }
    pull = approval._verify_review_merge_remote(
        client, config, identity, commit_field="merge_commit", tree_field="tree",
        first_parent=SYNC_MERGE, label="approved deployment repair", state="closed",
        work_actor=arguments.get("work_actor", ""),
    )
    if pull["head"].get("ref") != BRANCH or pull.get("merged") is not True:
        raise approval.ApprovalError("Continuation repair branch changed")
    files = approval._pages(client, approval._path(REPOSITORY, f"pulls/{pull['number']}/files"))
    if (
        len(files) != pull.get("changed_files") or not files
        or any(item.get("filename") not in ALLOWED_PATHS or item.get("status") not in {"added", "modified"} for item in files)
    ):
        raise approval.ApprovalError("Continuation repair changes unreviewed paths")
    api = deployment_evidence.ReadClient(client)
    try:
        repair_readiness = deployment_evidence.readiness.verify_published(
            api, REPOSITORY, pull["number"], identity["feature_head"], current,
            allowed_mergers=[owner, *([arguments["work_actor"]] if arguments.get("work_actor") else [])],
        )
    except deployment_evidence.readiness.ReadinessError as exc:
        raise approval.ApprovalError(str(exc)) from exc
    deployment_evidence.require_feature_runway(api, REPOSITORY, repair_readiness)
    main_run = approval._main_ci_run(client, config, current)
    _failed_attempt(client, config)
    # This workflow had no dispatch entry point before this repair. Any other
    # dispatch, including a failed or cancelled one, needs another reviewed repair.
    runs = client.get(approval._path(REPOSITORY, f"actions/workflows/{WORKFLOW.split('/')[-1]}/runs"),
                      {"event": "workflow_dispatch", "branch": "main", "per_page": "100"})
    selected = runs.get("workflow_runs", [])
    if runs.get("total_count") != 1 or len(selected) != 1:
        raise approval.ApprovalError("Approved continuation is missing or has already been attempted")
    run = selected[0]
    if (
        run.get("id") != run_id or run.get("run_attempt") != 1
        or run.get("head_sha") != current or run.get("head_branch") != "main"
        or run.get("event") != "workflow_dispatch"
        or run.get("status") not in {"in_progress", "queued"} or run.get("conclusion") is not None
        or approval._workflow_path(run) != WORKFLOW
        or approval._repo_name(run.get("repository")) != REPOSITORY
        or approval._repo_name(run.get("head_repository")) != REPOSITORY
        or approval._login(run.get("actor")) != owner or approval._login(run.get("triggering_actor")) != owner
    ):
        raise approval.ApprovalError("Approved continuation run identity changed")
    sync = client.get(approval._path(REPOSITORY, "pulls/52"))
    if (
        sync.get("merged") is not True or sync.get("merge_commit_sha") != SYNC_MERGE
        or sync.get("head", {}).get("sha") != SYNC_HEAD
        or approval._login(sync.get("merged_by")) != owner
        or approval._login(sync.get("user")) != owner
    ):
        raise approval.ApprovalError("The already-approved synchronization merge changed")
    report = approval._authorize_current_recovery(
        {"pull_request": sync}, version="0.6.2", number=52, sync_head_commit=SYNC_HEAD,
        current_main=current, **arguments,
        **({"transport": None} if "transport" not in arguments else {}),
        **({"sleeper": approval.time.sleep} if "sleeper" not in arguments else {}),
    )
    if report["approval_nonce"] != APPROVAL_NONCE or report["sync_head_tree"] != SYNC_TREE:
        raise approval.ApprovalError("The original v0.6.2 approval changed")
    # Reuse the same live review parser for PR52, including new serious findings
    # or explicit changes-requested reviews after its merge. No new approval.
    try:
        deployment_evidence.readiness._validate_review_evidence(
            deployment_evidence.readiness._review_evidence(api, REPOSITORY, 52)
        )
    except deployment_evidence.readiness.ReadinessError as exc:
        raise approval.ApprovalError(str(exc)) from exc
    report["continuation"] = {
        "failed_run_id": FAILED_RUN, "failed_run_attempt": 1,
        "workflow_commit": current, "run_id": run_id, "run_attempt": 1,
        "repair_readiness": repair_readiness, "main_validation_run_id": main_run["id"],
        "main_validation_run_attempt": main_run["run_attempt"],
    }
    return report
