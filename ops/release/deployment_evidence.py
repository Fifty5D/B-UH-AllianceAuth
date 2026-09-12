"""One evidence policy at review, authorization, and the queued SSH boundary.

Temporary screenshots qualify a feature merge. For the *pinned* recovered v0.6.2
they are historical provenance in the approved, bot-published release record;
they are not claimed to be downloadable. The retained exact-release validation
and preflight remain mandatory. All other releases retain live artifact checks.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import readiness  # noqa: E402

import platform_approval as approval  # noqa: E402


# Longer than the reusable operation's 65 minute timeout. Rechecked after queueing.
EVIDENCE_RUNWAY = dt.timedelta(minutes=90)


class ReadClient(readiness.GitHubClient):
    """Share the bounded approval transport, including read-only GraphQL."""

    def __init__(self, client: approval.GitHubClient) -> None:
        super().__init__(client.api_url, client.token)
        self.client = client

    def get_json(self, path, query=None):
        return self.client.get(path, query)

    def graphql(self, query, variables):
        return approval._graphql_read(self.client, query, variables)["data"]


def require_runway(artifact: Mapping[str, Any]) -> None:
    if artifact.get("expired") is not False or approval._timestamp(
        "Required deployment evidence expiry", artifact.get("expires_at")
    ) <= dt.datetime.now(dt.timezone.utc) + EVIDENCE_RUNWAY:
        raise approval.ApprovalError(
            "Required deployment evidence lacks the 90-minute execution window"
        )


def require_feature_runway(api, repository, published) -> None:
    identities = []
    if published["readiness"] is not None:
        identities.append((published["readiness"]["artifact_id"], published["readiness"]["artifact_digest"]))
    if published["preview_required"]:
        identities.extend((published["preview"][kind + "_artifact_id"], published["preview"][kind + "_artifact_digest"])
                          for kind in ("manifest", "evidence"))
    for identity, digest in identities:
        artifact = api.get_json(f"/repos/{repository}/actions/artifacts/{identity}")
        if artifact.get("id") != identity or artifact.get("digest") != digest:
            raise approval.ApprovalError("Required feature artifact identity changed")
        require_runway(artifact)


def verify_recovery_feature(client, config, published, *, work_actor="") -> None:
    """Verify the live identity/review behind the approved historical snapshot.

    No expired artifact is downloaded or fabricated. The caller must first
    authenticate the snapshot through the exact release's retained recovery
    attestation, readiness comment and (when deploying) merge approval marker.
    """
    contract = approval.validation_recovery.load_contract()
    release = contract["release"]
    if (
        config.repository != contract["repository"]
        or config.version != "0.6.2"
        or config.release_commit != release["commit"]
        or config.source_sha != release["source_commit"]
        or published["feature_pr"] != {
            "number": contract["feature"]["pull_request"],
            "head_sha": contract["feature"]["head_commit"],
        }
    ):
        raise approval.ApprovalError("Historical feature evidence is outside v0.6.2 recovery")
    api = ReadClient(client)
    repository = config.repository
    number = published["feature_pr"]["number"]
    head = published["feature_pr"]["head_sha"]
    try:
        pull = readiness._merged_feature_pr(
            api.get_json(f"/repos/{repository}/pulls/{number}"),
            repository=repository, pr=number, feature_head=head,
            merge_source_commit=config.source_sha, context="approved feature PR",
        )
        if pull["merged_by"] != published["merger"] or not approval._allowed_operator(
            pull["merged_by"], config, work_actor
        ):
            raise readiness.ReadinessError("approved feature merger changed")
        readiness._verified_feature_commit_pr_association(
            api, repository, number, head, config.source_sha, pull
        )
        source = approval._read_commit(client, config, config.source_sha)
        parents = source.get("parents", [])
        if len(parents) != 2 or parents[1].get("sha") != head:
            raise readiness.ReadinessError("approved source is not the exact feature merge")
        pointer = readiness._readiness_pointer(api, repository, number, head)
        if any(pointer.get(key) != value for key, value in published["readiness"].items()):
            raise readiness.ReadinessError("historical readiness pointer changed")
        reviews = readiness._validate_review_evidence(
            readiness._review_evidence(api, repository, number)
        )
        if reviews["digest"] != published["review_digest"]:
            raise readiness.ReadinessError("approved feature review evidence changed")
        validation = published["feature_validation"]
        readiness._newest_qualifying_workflow_run(
            api, repository, number, head,
            expected_run_id=validation["workflow_run_id"],
            expected_run_attempt=validation["workflow_run_attempt"],
            workflow=readiness.SOURCE_WORKFLOW, event="pull_request",
            context="approved feature validation",
        )
        readiness._verified_workflow_run(
            api, repository, number, head,
            run_id=validation["workflow_run_id"],
            run_attempt=validation["workflow_run_attempt"],
            workflow=readiness.SOURCE_WORKFLOW, event="pull_request",
            context="approved feature validation", allow_postmerge_empty_association=True,
        )
        check = api.get_json(f"/repos/{repository}/check-runs/{validation['check_run_id']}")
        if (
            check.get("id") != validation["check_run_id"]
            or check.get("head_sha") != head
            or check.get("name") != readiness.REQUIRED_CHECK
            or check.get("status") != "completed"
            or check.get("conclusion") != "success"
            or check.get("app", {}).get("slug") != "github-actions"
        ):
            raise readiness.ReadinessError("approved feature validation check changed")
    except readiness.ReadinessError as exc:
        raise approval.ApprovalError(str(exc)) from exc


def verify(client, config, report, *, work_actor="") -> None:
    """Recheck the same required evidence at every authorization boundary."""
    published = approval._feature_readiness(report["feature_readiness"], config)
    if report.get("validation_mode") == "published-release-sync-update":
        verify_recovery_feature(client, config, published, work_actor=work_actor)
        contract = approval.validation_recovery.load_contract()
        recovery = contract["partial_publication"]
        artifact = recovery["artifact"]
        retained = approval._artifact(
            client, config, recovery["run_id"], artifact["name"],
            head_sha=contract["publication_repair"]["base_commit"],
            expected_id=artifact["id"], expected_digest=artifact["digest"],
        )
        require_runway(retained)
    else:
        try:
            actual = readiness.verify_published(
                ReadClient(client), config.repository,
                published["feature_pr"]["number"], published["feature_pr"]["head_sha"],
                config.source_sha, allowed_mergers=[config.owner, *([work_actor] if work_actor else [])],
                allow_first_introduction=published["feature_pr"]["number"] == 44,
                first_introduction_pr=44 if published["feature_pr"]["number"] == 44 else None,
            )
        except readiness.ReadinessError as exc:
            raise approval.ApprovalError(str(exc)) from exc
        if actual != published:
            raise approval.ApprovalError("Live feature readiness differs from approval")
        require_feature_runway(ReadClient(client), config.repository, actual)
    retained = approval._artifact(
        client, config, report["preflight_run_id"], report["preflight_artifact"],
        expected_id=report["preflight_artifact_id"],
        expected_digest=report["preflight_artifact_digest"],
    )
    require_runway(retained)
