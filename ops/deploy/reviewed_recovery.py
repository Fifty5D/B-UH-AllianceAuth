"""Complete the pinned rollback after an explicit review of recovered API errors.

Historical evidence is retained, not declared clean. This requires the reviewed
receiver repair. Completion needs a separate exact owner approval.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from . import recovery_audit as audit
from . import worker_recovery as recovery
from .collect_worker_recovery import read_file
from .contracts import (
    DeploymentError,
    ReceiverConfig,
    extract_archive,
    load_validated_bundle,
)
from .docker_host import DockerHost
from .engine import _atomic_write
from .receiver import _open_lock, _verify_root_owned_ancestors


TASKS = {
    "structures.tasks.update_structures_assets_for_owner",
    "structures.tasks.fetch_notification_for_owner",
    "moonmining.tasks.update_refineries_from_esi_for_owner",
    "moonmining.tasks.fetch_notifications_from_esi_for_owner",
}
RECEIPT_NAME = f"worker-recovery-{recovery.ATTEMPT}.json"


class ReviewedHealthError(DeploymentError):
    def __init__(self, checks):
        super().__init__(
            "Reviewed recovery health checks failed; inspect the combined report"
        )
        self.checks = checks


def require(condition, message):
    if not condition:
        raise DeploymentError(message)


def stamp(value):
    require(isinstance(value, str), "Missing evidence timestamp")
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(value.tzinfo is not None, "Evidence timestamp needs an offset")
    return value.astimezone(timezone.utc)


def read_json(path, digest):
    require(
        re.fullmatch(r"[0-9a-f]{64}", digest or "") is not None, "Invalid evidence hash"
    )
    metadata, raw = read_file(path, maximum=1024 * 1024)
    require(
        raw is not None and metadata.get("sha256") == digest, "Reviewed evidence changed"
    )
    value = json.loads(raw)
    require(isinstance(value, dict), "Evidence must be a JSON object")
    return value


def validate_history(evidence):
    """Account for every ERROR record, not merely selected sample signatures."""
    require(
        evidence.get("schema_version") == 1 and evidence.get("all_streams_read") is True,
        "Historical log read was incomplete",
    )
    require(evidence.get("since") == audit.LOG_SINCE, "Original incident interval changed")
    containers = evidence.get("containers", [])
    require(len(containers) == 6, "Historical worker topology changed")
    identities = set()
    service_workers = 0
    for container in containers:
        identity = container.get("container", "")
        require(
            re.fullmatch(r"[0-9a-f]{12}", identity) is not None
            and identity not in identities,
            "Historical container identity is invalid",
        )
        identities.add(identity)
        require(
            container.get("complete") is True
            and container.get("exit_code") == 0
            and container.get("stop_reason") is None
            and container.get("groups_truncated") is False
            and container.get("lines_read", 0) > 0,
            "Historical source is partial or empty",
        )
        levels = container.get("severity_log_records", {})
        require(
            not levels.get("CRITICAL") and not levels.get("FATAL"),
            "Unreviewed critical historical error",
        )
        accounted = 0
        owner_denials = exhausted = temporary = http403 = http503 = exceptions = 0
        has_discord = False
        for group in container.get("groups", []):
            kind, detail, count = (
                group.get("kind"),
                group.get("detail"),
                group.get("log_records"),
            )
            require(type(count) is int and count > 0, "Invalid historical record count")
            if kind == "task_outcome":
                require(
                    detail.get("task") in TASKS
                    and detail.get("outcome") == "raised unexpected",
                    "Unreviewed historical task failure",
                )
                accounted += count
            elif kind == "discord_response":
                has_discord = True
                if detail == {
                    "http_status": 403,
                    "is_reviewed_owner": True,
                    "discord_code": 50013,
                }:
                    owner_denials += count
                else:
                    require(
                        detail
                        in (
                            {
                                "http_status": 429,
                                "is_reviewed_owner": False,
                                "discord_code": None,
                            },
                            {
                                "http_status": 503,
                                "is_reviewed_owner": False,
                                "discord_code": 0,
                            },
                        ),
                        "Unreviewed Discord denial or response",
                    )
                    temporary += count
                accounted += count
            elif kind == "discord_operation_failure":
                has_discord = True
                require(
                    detail.get("operation") == "update_nickname",
                    "Discord role failure is not covered",
                )
                if (
                    detail.get("is_reviewed_owner") is True
                    and detail.get("retry_logged") is False
                ):
                    exhausted += count
                    accounted += count
                else:
                    require(
                        detail.get("retry_logged") is True,
                        "Unreviewed exhausted Discord operation",
                    )
            elif kind == "http_status":
                require(detail in {"403", "503"}, "Unreviewed historical HTTP status")
                if detail == "403":
                    http403 += count
                else:
                    http503 += count
            elif kind == "exception_class":
                require(
                    detail == "requests.exceptions.HTTPError", "Unreviewed exception class"
                )
                exceptions += count
            else:
                raise DeploymentError("Unreviewed historical evidence category")
        require(
            accounted == levels.get("ERROR", 0),
            "Historical ERROR records are not fully accounted for",
        )
        if has_discord:
            service_workers += 1
            require(
                owner_denials > 0
                and http403 == owner_denials + exhausted
                and exceptions == http403 + http503
                and http503 <= temporary,
                "Discord response and exception accounting differs",
            )
        else:
            require(
                not (http403 or http503 or exceptions),
                "Unexpected non-Discord HTTP evidence",
            )
    require(service_workers == 1, "Historical services worker identity differs")
    return identities


def validate_review(review, evidence, *, now):
    require(
        review.get("schema_version") == 1 and review.get("attempt_id") == recovery.ATTEMPT,
        "Review belongs to a different recovery",
    )
    require(
        review.get("previous_runtime_sha256") == recovery.INSTALLED_CLASSIFIER_RUNTIME,
        "Installed receiver review differs",
    )
    require(
        review.get("historical_assessment") == "api-errors-followed-by-successful-syncs",
        "Historical assessment is missing",
    )
    require(
        review.get("historical_esi_root_cause") == "not-recorded",
        "Review must preserve the unknown historical ESI cause",
    )
    cutoff = stamp(evidence["until"])
    require(
        stamp(audit.LOG_SINCE) < cutoff <= stamp(review["sync_evidence_at"]) <= now,
        "Historical and recovery evidence chronology differs",
    )
    # Historical evidence does not expire into another collection loop. Current
    # status is queried and bounded separately on every verify/complete call.
    for name in ("structures_owners", "moonmining_owners", "refineries"):
        values = review.get(name)
        require(
            isinstance(values, list)
            and values
            and len(values) <= 500
            and all(type(item) is int and item > 0 for item in values)
            and len(values) == len(set(values)),
            "Reviewed sync population is invalid",
        )
    return cutoff


def live_sync_probe(host):
    # Fixed SELECTs only; no tasks, token refresh, API access or model saves.
    code = (
        "import json;from django.utils.timezone import now;"
        "from structures.models import Owner as S;"
        "from moonmining.models import Owner as M,Refinery as R;"
        "print('BUH_SYNC='+json.dumps({'captured_at':now().isoformat(),"
        "'structures_owners':list(S.objects.order_by('pk').values('pk','is_active','is_up',"
        "'is_included_in_service_status','structures_last_update_at','assets_last_update_at',"
        "'notifications_last_update_at','forwarding_last_update_at')[:501]),"
        "'moonmining_owners':list(M.objects.order_by('pk').values('pk','is_enabled',"
        "'last_update_at','last_update_ok')[:501]),"
        "'refineries':list(R.objects.order_by('pk').values('pk','owner_id',"
        "'ledger_last_update_at','ledger_last_update_ok')[:501])},default=str))"
    )
    output = host._manage_live(
        "shell", "-c", code, context="Read-only recovered data sync status"
    )
    records = [
        line.removeprefix("BUH_SYNC=")
        for line in output.splitlines()
        if line.startswith("BUH_SYNC=")
    ]
    require(len(records) == 1, "Live sync report is missing or ambiguous")
    return json.loads(records[0])


def validate_sync(status, review, cutoff, *, now):
    require(
        abs((stamp(status["captured_at"]) - now).total_seconds()) <= 120,
        "Live sync report is stale",
    )
    fields = {
        "structures_owners": (
            {"is_active", "is_up", "is_included_in_service_status"},
            {
                "structures_last_update_at": 7200,
                "assets_last_update_at": 7200,
                "notifications_last_update_at": 1800,
                "forwarding_last_update_at": 1800,
            },
        ),
        "moonmining_owners": ({"is_enabled", "last_update_ok"}, {"last_update_at": 1800}),
        "refineries": ({"ledger_last_update_ok"}, {"ledger_last_update_at": 7200}),
    }
    for name, (flags, clocks) in fields.items():
        rows = status.get(name, [])
        require(
            len(rows) == len(review[name])
            and {row.get("pk") for row in rows} == set(review[name]),
            "Recovered data population changed",
        )
        for row in rows:
            require(
                all(row.get(flag) is True for flag in flags),
                "A reviewed data sync is disabled or unsuccessful",
            )
            if name == "refineries":
                require(
                    row.get("owner_id") in review["moonmining_owners"],
                    "Refinery owner changed",
                )
            for field, maximum_age in clocks.items():
                updated = stamp(row.get(field))
                require(
                    cutoff < updated <= now
                    and (now - updated).total_seconds() <= maximum_age,
                    "A data sync has not recovered or is stale",
                )


def verify(host, value, bundle, review, evidence):
    recovery.prepare_verification(host, value, bundle)
    cutoff = validate_review(review, evidence, now=datetime.now(timezone.utc))
    identities = validate_history(evidence)
    live = set()
    for service in host.config.auth_services:
        if service not in {host.config.gunicorn_service, host.config.beat_service}:
            live.update(
                item[:12]
                for item in host._running_service_containers(
                    service, context="Reviewed worker identity"
                )
            )
    require(
        live == identities, "Historical evidence no longer matches the retained workers"
    )
    sync = {}

    def check_sync():
        sync.update(live_sync_probe(host))
        validate_sync(sync, review, cutoff, now=datetime.now(timezone.utc))

    # Explicit reviewed boundary: historical evidence remains attached to the
    # result. Everything after this fixed boundary uses the ordinary strict scan.
    host.log_since = evidence["until"]
    checks = audit.run_checks(
        [
            *recovery.restored_file_checks(host),
            ("recovered-data-syncs", check_sync),
            *host.restored_health_checks(bundle, set()),
        ]
    )
    if not all(check["result"] == "passed" for check in checks):
        raise ReviewedHealthError(checks)
    return {
        "schema_version": 1,
        "attempt_id": recovery.ATTEMPT,
        "result": "restored-production-verified-with-reviewed-history",
        "original_log_since": audit.LOG_SINCE,
        "reviewed_log_until": evidence["until"],
        "historical_interval_clean": False,
        "historical_esi_root_cause": "not-recorded",
        "historical_log_evidence_sha256": review["historical_log_evidence_sha256"],
        "current_sync_status": sync,
        "checks": checks,
        "cleanup": "not-run",
        "deployment_performed": False,
        "database_restored": False,
        "migrations_reversed": False,
        "retained_plan_sha256": recovery.PLAN_SHA256,
        "original_journal_sha256": recovery.JOURNAL_SHA256,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("verify", "complete"))
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--review-sha256", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument(
        "--before-install",
        action="store_true",
        help="Verify staged code against the original installed PR #63 receiver; never complete",
    )
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    lock = None
    cleanup = "not-run"
    try:
        require(os.geteuid() == 0, "Root-owned reviewed source is required")
        require(
            not args.before_install or args.operation == "verify",
            "Staged verification cannot complete recovery",
        )
        if args.operation == "complete":
            require(
                args.confirm
                == f"COMPLETE REVIEWED ROLLBACK {recovery.ATTEMPT} {args.review_sha256}",
                "Separate owner approval for this exact recovery review is required",
            )
        review = read_json(args.review, args.review_sha256)
        require(
            review.get("receiver_sha256") == args.runtime_sha256,
            "Reviewed receiver payload differs",
        )
        evidence = read_json(
            args.review.parent / "historical-log-details.json",
            review["historical_log_evidence_sha256"],
        )
        validate_history(evidence)
        validate_review(review, evidence, now=datetime.now(timezone.utc))
        config = ReceiverConfig.load(Path("/etc/buh-platform-v2/receiver.json"))
        lock = _open_lock(config)

        def verify_receiver():
            if args.before_install:
                recovery.verify_pins(
                    args.runtime_sha256,
                    installed_sha256=recovery.INSTALLED_CLASSIFIER_RUNTIME,
                )
                recovery.verify_classifier_repair(recovery.INSTALLED_CLASSIFIER_RUNTIME)
            else:
                recovery.verify_pins(args.runtime_sha256)
                recovery.verify_exhaustion_repair(args.runtime_sha256)

        verify_receiver()
        loaded = DockerHost.load_incomplete_plan(config)
        require(
            loaded is not None, "Pinned recovery plan is missing; do not replay completion"
        )
        receipt = config.state_dir / RECEIPT_NAME
        require(
            not receipt.exists() and not receipt.is_symlink(),
            "Recovery receipt exists; do not repeat",
        )
        metadata, archive = read_file(args.archive, maximum=8 * 1024 * 1024)
        require(
            archive is not None and metadata.get("sha256") == audit.ARCHIVE_SHA256,
            "Immutable verification archive changed",
        )
        with tempfile.TemporaryDirectory(prefix="buh-reviewed-recovery-") as tmp:
            payload = Path(tmp) / "payload"
            extract_archive(archive, payload)
            bundle = load_validated_bundle(payload, config)
            host, value = loaded
            result = verify(host, value, bundle, review, evidence)
            # Verify pins again before any completion operation.
            verify_receiver()
            if args.operation == "complete":
                # Retain the exact reviewed evidence independently of temporary
                # staging before any ordinary rollback completion can clean slots.
                evidence_dir = config.state_dir / f"reviewed-history-{args.review_sha256}"
                evidence_dir.mkdir(mode=0o700, exist_ok=True)
                _verify_root_owned_ancestors(evidence_dir / "review.json")
                for name, path, digest in (
                    ("review.json", args.review, args.review_sha256),
                    (
                        "historical-log-details.json",
                        args.review.parent / "historical-log-details.json",
                        review["historical_log_evidence_sha256"],
                    ),
                ):
                    read_json(path, digest)  # Recheck immediately before retention.
                    raw = read_file(path, maximum=1024 * 1024)[1]
                    require(
                        hashlib.sha256(raw).hexdigest() == digest,
                        "Evidence changed before retention",
                    )
                    target = evidence_dir / name
                    if target.exists() or target.is_symlink():
                        require(
                            read_file(target)[0].get("sha256") == digest,
                            "Retained review evidence changed",
                        )
                    else:
                        _atomic_write(target, raw)
                _atomic_write(
                    evidence_dir / "verification.json",
                    (json.dumps(result, sort_keys=True) + "\n").encode(),
                )
                cleanup = "not-established"
                result["recovery"] = host.rollback(bundle, value["phase"])
                cleanup = result["cleanup"] = "passed"
            result.update(
                review_sha256=args.review_sha256,
                runtime_sha256=args.runtime_sha256,
                installed_runtime_sha256=(
                    recovery.INSTALLED_CLASSIFIER_RUNTIME
                    if args.before_install
                    else args.runtime_sha256
                ),
                archive_sha256=hashlib.sha256(archive).hexdigest(),
            )
            if args.before_install:
                result["result"] = "staged-recovery-verification-passed"
            if args.operation == "complete":
                _atomic_write(receipt, (json.dumps(result, sort_keys=True) + "\n").encode())
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, DeploymentError) as error:
        print(
            json.dumps(
                {
                    "result": "failed",
                    "cleanup": cleanup,
                    "preserve_resources": True,
                    **(
                        {"checks": error.checks}
                        if isinstance(error, ReviewedHealthError)
                        else {}
                    ),
                    "error": str(error)[:500]
                    if isinstance(error, DeploymentError)
                    else "Invalid or unavailable recovery evidence",
                }
            )
        )
        return 1
    finally:
        if lock is not None:
            os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
