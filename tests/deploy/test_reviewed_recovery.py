"""Recovered history cannot erase fresh failures or authorize cleanup by itself."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from ops.deploy import reviewed_recovery as reviewed
from ops.deploy.contracts import DeploymentError
from ops.deploy.docker_host import LogScanError
from ops.deploy.recovered_logs import ReviewedDockerHost
from tests.deploy.test_docker_host import discord_exhausted_record, make_config


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
CUTOFF = "2026-09-14T18:06:30+00:00"
RUNTIME = "a" * 64


def fixture():
    def group(kind, detail, count=1):
        return {"kind": kind, "detail": detail, "log_records": count}

    containers = [
        {
            "container": f"{index:012x}",
            "complete": True,
            "exit_code": 0,
            "stop_reason": None,
            "groups_truncated": False,
            "lines_read": 100,
            "severity_log_records": {"ERROR": 1},
            "groups": [
                group(
                    "task_outcome",
                    {
                        "task": "structures.tasks.update_structures_assets_for_owner",
                        "outcome": "raised unexpected",
                    },
                )
            ],
        }
        for index in range(1, 7)
    ]
    containers[-1]["severity_log_records"] = {"ERROR": 4, "WARNING": 2}
    containers[-1]["groups"] = [
        group(
            "discord_response",
            {"http_status": 403, "is_reviewed_owner": True, "discord_code": 50013},
            2,
        ),
        group(
            "discord_response",
            {"http_status": 429, "is_reviewed_owner": False, "discord_code": None},
        ),
        group(
            "discord_operation_failure",
            {
                "operation": "update_nickname",
                "is_reviewed_owner": True,
                "retry_logged": True,
            },
            2,
        ),
        group(
            "discord_operation_failure",
            {
                "operation": "update_nickname",
                "is_reviewed_owner": True,
                "retry_logged": False,
            },
        ),
        group("exception_class", "requests.exceptions.HTTPError", 3),
        group("http_status", "403", 3),
    ]
    evidence = {
        "schema_version": 1,
        "all_streams_read": True,
        "since": reviewed.audit.LOG_SINCE,
        "until": CUTOFF,
        "containers": containers,
    }
    review = {
        "schema_version": 1,
        "attempt_id": reviewed.recovery.ATTEMPT,
        "previous_runtime_sha256": reviewed.recovery.INSTALLED_CLASSIFIER_RUNTIME,
        "receiver_sha256": RUNTIME,
        "historical_assessment": "api-errors-followed-by-successful-syncs",
        "historical_esi_root_cause": "not-recorded",
        "historical_log_evidence_sha256": "b" * 64,
        "sync_evidence_at": "2026-09-14T19:27:35+00:00",
        "current_evidence_policy": "scheduled-sync-and-retained-errors-v1",
        "asset_recoveries": [{"previous_character_pk": 5, "eve_character_id": 90000001}],
        "structures_owners": [101],
        "moonmining_owners": [101],
        "refineries": [10001],
    }
    clock = (NOW - timedelta(minutes=5)).isoformat()
    status = {
        "captured_at": NOW.isoformat(),
        "structures_owners": [
            {
                "pk": 101,
                "is_active": True,
                "is_up": True,
                "is_included_in_service_status": True,
                "structures_last_update_at": clock,
                "assets_last_update_at": clock,
                "notifications_last_update_at": clock,
                "forwarding_last_update_at": clock,
            }
        ],
        "moonmining_owners": [
            {"pk": 101, "is_enabled": True, "last_update_ok": True, "last_update_at": clock}
        ],
        "refineries": [
            {
                "pk": 10001,
                "owner_id": 101,
                "ledger_last_update_at": clock,
                "ledger_last_update_ok": True,
            }
        ],
    }

    def schedule(seconds):
        return [
            {
                "enabled": True,
                "last_run_at": clock,
                "interval__every": seconds,
                "interval__period": "seconds",
                "crontab_id": None,
                "solar_id": None,
                "clocked_id": None,
            }
        ]

    requested = (NOW - timedelta(minutes=10)).isoformat()
    status.update(
        log_timezone="UTC",
        moon_tax_config=[{"audit_interval_hours": 4}],
        moon_tax_schedule=schedule(3600),
        completed_audits=[
            {
                "status": "COMPLETE",
                "queued_at": requested,
                "source_refresh_requested_at": requested,
                "finished_at": clock,
                "warning_schema_valid": True,
                "warning_count": 0,
                "warning_categories": {},
                "has_error": False,
                "price_snapshot_count": 0,
                "incomplete_price_snapshot_count": 0,
                "incomplete_price_snapshot_schema_valid": True,
                "jita_depth_incomplete_price_snapshot_count": 0,
            }
        ],
        asset_stale_minutes=240,
        memberaudit_schedule=schedule(900),
        asset_characters=[
            {"pk": 123, "is_disabled": False, "eve_character__character_id": 90000001}
        ],
        asset_status=[
            {
                "character_id": 123,
                "is_success": True,
                "has_token_error": False,
                "run_finished_at": clock,
            }
        ],
    )
    return evidence, review, status


class ReviewedRecoveryTests(unittest.TestCase):
    def test_historical_errors_remain_reviewed_and_do_not_expire_into_recollection(self):
        evidence, review, status = fixture()
        self.assertEqual(len(reviewed.validate_history(evidence)), 6)
        cutoff = reviewed.validate_review(review, evidence, now=NOW)
        reviewed.validate_sync(status, review, cutoff, now=NOW)
        self.assertEqual(cutoff.isoformat(), CUTOFF)

    def test_incomplete_unknown_and_unaccounted_history_blocks(self):
        base, _, _ = fixture()
        cases = []
        for field, value in (
            ("complete", False),
            ("groups_truncated", True),
            ("lines_read", 0),
        ):
            candidate = deepcopy(base)
            candidate["containers"][0][field] = value
            cases.append(candidate)
        candidate = deepcopy(base)
        candidate["containers"][0]["severity_log_records"]["ERROR"] += 1
        cases.append(candidate)
        for detail in (
            {"task": "unrelated.task", "outcome": "raised unexpected"},
            {"task": next(iter(reviewed.TASKS)), "outcome": "succeeded"},
        ):
            candidate = deepcopy(base)
            candidate["containers"][0]["groups"][0]["detail"] = detail
            cases.append(candidate)
        candidate = deepcopy(base)
        candidate["containers"][-1]["groups"][0]["detail"]["is_reviewed_owner"] = False
        cases.append(candidate)
        candidate = deepcopy(base)
        candidate["containers"][-1]["groups"][3]["detail"]["operation"] = "update_groups"
        cases.append(candidate)
        for index, evidence in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(DeploymentError):
                reviewed.validate_history(evidence)

    def test_fresh_timestamps_alone_do_not_establish_success_or_population(self):
        _, review, base = fixture()
        cases = []
        for name, field, value in (
            ("structures_owners", "is_up", False),
            ("moonmining_owners", "last_update_ok", False),
            ("refineries", "ledger_last_update_ok", False),
            ("refineries", "owner_id", 102),
            ("structures_owners", "assets_last_update_at", CUTOFF),
            (
                "structures_owners",
                "notifications_last_update_at",
                (NOW - timedelta(hours=1)).isoformat(),
            ),
            (
                "refineries",
                "ledger_last_update_at",
                (NOW + timedelta(minutes=1)).isoformat(),
            ),
        ):
            status = deepcopy(base)
            status[name][0][field] = value
            cases.append(status)
        status = deepcopy(base)
        status["refineries"] = []
        cases.append(status)
        for index, status in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(DeploymentError):
                reviewed.validate_sync(status, review, reviewed.stamp(CUTOFF), now=NOW)

    def test_exact_hashes_and_offset_timestamps_are_required(self):
        with mock.patch.object(
            reviewed, "read_file", return_value=({"sha256": "b" * 64}, b"{}")
        ):
            with self.assertRaises(DeploymentError):
                reviewed.read_json(Path("unused"), "a" * 64)
        for value in (None, "2026-09-14T19:00:00"):
            with self.assertRaises(DeploymentError):
                reviewed.stamp(value)

    def exercise_main(self, operation, *, fail=None, before_install=False, gap=False):
        evidence, review, status = fixture()
        fresh_since = (NOW - timedelta(hours=6)).isoformat()
        if gap:
            review["retained_log_gap"] = {
                "policy": "documented-worker-log-rotation-v1",
                "unverified_since": CUTOFF,
                "fresh_scan_since": fresh_since,
                "diagnostic_report_sha256": "d" * 64,
                "retained_grouping_sha256": "e" * 64,
            }
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            historical = root / "historical-log-details.json"
            historical.write_text(json.dumps(evidence, indent=2))
            historical_bytes = historical.read_bytes()
            review["historical_log_evidence_sha256"] = hashlib.sha256(
                historical_bytes
            ).hexdigest()
            review_path = root / "review.json"
            review_path.write_text(json.dumps(review, indent=2))
            review_hash = hashlib.sha256(review_path.read_bytes()).hexdigest()
            archive = root / "archive.tar.gz"
            archive.write_bytes(b"synthetic immutable archive")
            config = make_config(root)
            config.state_dir.mkdir(parents=True, exist_ok=True)
            host = ReviewedDockerHost(config)
            host.original_dockerfile = root / "original"
            host.traffic_switch_started = host.migration_started = True
            host.previous_web_slots = ("synthetic-previous-slot",)
            host.candidate_web_slots = ("synthetic-candidate-slot",)
            host.recovery_baseline_verified = True
            events = []
            ids = [entry["container"] for entry in evidence["containers"]]

            def containers(service, **kwargs):
                return ids[-1:] if service.endswith("services") else ids[:-1]

            def log_check():
                self.assertEqual(host.log_since, fresh_since if gap else CUTOFF)
                events.append("logs")
                if fail == "logs":
                    raise LogScanError(["synthetic new task failure"])
                return host._classify_log_batch(
                    [(config.worker_service, discord_exhausted_record()[0])],
                    {config.worker_service},
                    "rollback",
                )

            def fresh_coverage():
                events.append("coverage")
                if fail == "coverage":
                    raise DeploymentError("Fresh worker log window is not retained")
                host.worker_log_start_coverage = [
                    {"container": identity, "first_at": fresh_since if gap else CUTOFF}
                    for identity in ids
                ]

            def health(*args):
                checks = [
                    ("retained-interval-logs", log_check),
                    ("public-http", lambda: events.append("http")),
                ]
                checks.insert(0, ("worker-log-start-coverage", host._verify_worker_log_start_coverage))
                return checks

            def bytes_read(path, **kwargs):
                raw = path.read_bytes()
                return {"sha256": hashlib.sha256(raw).hexdigest()}, raw

            for name, value in (
                ("_activate_upstream", None),
                ("_public_smoke_checks", None),
                ("_restore_candidate_configuration", True),
                ("_restore_service_set", None),
                ("_discard_previous_image_pins", None),
                ("complete_recovery_plan", None),
            ):
                stack.enter_context(mock.patch.object(host, name, return_value=value))
            remove = stack.enter_context(
                mock.patch.object(
                    host,
                    "_remove_web_slots",
                    side_effect=lambda *args: events.append("cleanup"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    host, "_running_service_containers", side_effect=containers
                )
            )
            stack.enter_context(
                mock.patch.object(host, "restored_health_checks", side_effect=health)
            )
            stack.enter_context(
                mock.patch.object(
                    host, "_verify_worker_log_start_coverage", side_effect=fresh_coverage
                )
            )
            probe = stack.enter_context(
                mock.patch.object(reviewed, "live_sync_probe", return_value=status)
            )
            stack.enter_context(
                mock.patch.object(reviewed, "read_file", side_effect=bytes_read)
            )
            stack.enter_context(
                mock.patch.object(reviewed.recovery, "prepare_verification")
            )
            stack.enter_context(
                mock.patch.object(
                    reviewed.recovery, "restored_file_checks", return_value=[]
                )
            )
            pins = stack.enter_context(mock.patch.object(reviewed.recovery, "verify_pins"))
            stack.enter_context(
                mock.patch.object(reviewed.recovery, "verify_exhaustion_repair")
            )
            classifier = stack.enter_context(
                mock.patch.object(reviewed.recovery, "verify_classifier_repair")
            )
            stack.enter_context(mock.patch.object(reviewed, "_verify_root_owned_ancestors"))
            stack.enter_context(
                mock.patch.object(reviewed.ReceiverConfig, "load", return_value=config)
            )
            stack.enter_context(
                mock.patch.object(
                    reviewed.ReviewedDockerHost,
                    "load_incomplete_plan",
                    return_value=(host, {"phase": "failed"}),
                )
            )
            stack.enter_context(
                mock.patch.object(reviewed, "_open_lock", return_value=None)
            )
            stack.enter_context(mock.patch.object(reviewed, "extract_archive"))
            stack.enter_context(
                mock.patch.object(
                    reviewed, "load_validated_bundle", return_value=SimpleNamespace()
                )
            )
            stack.enter_context(
                mock.patch.object(
                    reviewed.audit,
                    "ARCHIVE_SHA256",
                    hashlib.sha256(archive.read_bytes()).hexdigest(),
                )
            )
            stack.enter_context(
                mock.patch.object(reviewed.os, "geteuid", return_value=0, create=True)
            )
            clock = stack.enter_context(mock.patch.object(reviewed, "datetime"))
            clock.now.return_value = NOW
            clock.fromisoformat.side_effect = datetime.fromisoformat
            if fail == "sync":
                status["moonmining_owners"][0]["last_update_ok"] = False
            if fail == "resync":
                changed = deepcopy(status)
                changed["moonmining_owners"][0]["last_update_ok"] = False
                probe.side_effect = [status, changed]
            if fail == "pins":
                pins.side_effect = DeploymentError("receiver changed")
            if fail == "receipt":
                (config.state_dir / reviewed.RECEIPT_NAME).write_text("{}")
            args = [
                operation,
                "--review",
                str(review_path),
                "--review-sha256",
                review_hash,
                "--runtime-sha256",
                RUNTIME,
                "--archive",
                str(archive),
            ]
            if operation == "complete" and fail != "approval":
                args += [
                    "--confirm",
                    f"COMPLETE REVIEWED ROLLBACK {reviewed.recovery.ATTEMPT} {review_hash}",
                ]
            if before_install:
                args.append("--before-install")
            output = stack.enter_context(mock.patch("sys.stdout", new_callable=io.StringIO))
            code = reviewed.main(args)
            result = json.loads(output.getvalue())
            if fail:
                self.assertEqual(code, 1, result)
                remove.assert_not_called()
                if fail in {"sync", "logs"}:
                    self.assertIn("http", events)
                    self.assertTrue(
                        any(check["result"] == "failed" for check in result["checks"])
                    )
                if fail in {"sync", "resync"}:
                    self.assertEqual(
                        result["sync_failures"]["findings"][0]["field"], "last_update_ok"
                    )
                    self.assertIn("current_sync_status", result)
                return
            self.assertEqual(code, 0, result)
            self.assertFalse(result["historical_interval_clean"])
            self.assertFalse(result["deployment_performed"])
            self.assertEqual(len(result["worker_log_start_coverage"]), 6)
            if gap:
                self.assertEqual(result["retained_log_gap"], review["retained_log_gap"])
                self.assertFalse(result["retained_interval_continuity_verified"])
            else:
                self.assertIsNone(result["retained_log_gap"])
                self.assertTrue(result["retained_interval_continuity_verified"])
            if before_install:
                self.assertEqual(result["result"], "staged-recovery-verification-passed")
                self.assertEqual(
                    result["installed_runtime_sha256"],
                    reviewed.recovery.INSTALLED_CLASSIFIER_RUNTIME,
                )
                self.assertEqual(classifier.call_count, 2)
            else:
                classifier.assert_not_called()
            host._restore_service_set.assert_not_called()
            if operation == "verify":
                remove.assert_not_called()
                self.assertFalse((config.state_dir / reviewed.RECEIPT_NAME).exists())
            else:
                self.assertEqual(result["cleanup"], "passed")
                self.assertEqual(events.count("logs"), 2)
                self.assertLess(events.index("logs"), events.index("cleanup"))
                retained = config.state_dir / f"reviewed-history-{review_hash}"
                self.assertEqual(
                    (retained / "historical-log-details.json").read_bytes(),
                    historical_bytes,
                )
                self.assertTrue((config.state_dir / reviewed.RECEIPT_NAME).exists())

    def test_verify_never_cleans_and_completion_uses_real_verified_rollback(self):
        self.exercise_main("verify", before_install=True)
        for operation in ("verify", "complete"):
            with self.subTest(operation=operation):
                self.exercise_main(operation)

    def test_failures_keep_resources_and_do_not_hide_later_health_results(self):
        self.exercise_main("verify", before_install=True, fail="logs")
        self.exercise_main("complete", before_install=True, fail="approval")
        for failure in ("approval", "pins", "receipt", "sync", "resync", "logs"):
            with self.subTest(failure=failure):
                self.exercise_main("complete", fail=failure)

    def test_documented_gap_requires_fresh_coverage_and_separate_completion(self):
        self.exercise_main("verify", gap=True, before_install=True)
        self.exercise_main("verify", gap=True, fail="coverage")
        self.exercise_main("complete", gap=True)

    def test_gap_review_rejects_missing_provenance_and_stale_window(self):
        evidence, review, _ = fixture()
        gap = {
            "policy": "documented-worker-log-rotation-v1",
            "unverified_since": CUTOFF,
            "fresh_scan_since": (NOW - timedelta(hours=6)).isoformat(),
            "diagnostic_report_sha256": "d" * 64,
            "retained_grouping_sha256": "e" * 64,
        }
        for field, value in (
            ("policy", "silent-rotation"),
            ("unverified_since", reviewed.audit.LOG_SINCE),
            ("fresh_scan_since", (NOW - timedelta(hours=25)).isoformat()),
            ("fresh_scan_since", (NOW - timedelta(hours=3)).isoformat()),
            ("diagnostic_report_sha256", "missing"),
        ):
            candidate = deepcopy(review)
            candidate["retained_log_gap"] = {**gap, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(DeploymentError):
                reviewed.validate_log_gap(candidate, evidence, now=NOW)

    def test_fresh_coverage_requires_each_original_worker_to_have_early_logs(self):
        evidence, _, _ = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            host = ReviewedDockerHost(make_config(Path(temporary)))
            ids = [entry["container"] for entry in evidence["containers"]]
            host.reviewed_worker_ids = frozenset(ids)
            host.log_start_coverage_since = (NOW - timedelta(hours=6)).isoformat()
            first = (NOW - timedelta(hours=6) + timedelta(minutes=1)).isoformat()
            def containers(service, **kwargs):
                return ids[-1:] if service.endswith("services") else ids[:-1]
            with mock.patch.object(host, "_running_service_containers", side_effect=containers), mock.patch.object(
                host, "_run", return_value=f"{first} example\n"
            ) as read:
                host._verify_worker_log_start_coverage()
                self.assertEqual(len(host.worker_log_start_coverage), 6)
                self.assertEqual(read.call_count, 6)
                read.return_value = ""
                with self.assertRaisesRegex(DeploymentError, "not retained"):
                    host._verify_worker_log_start_coverage()
