"""Incident regression cases: schedule-aware evidence and complete retained logs."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import time
import unittest

from ops.deploy.contracts import DeploymentError
from ops.deploy.docker_host import DockerHost, LogScanError
from ops.deploy.recovered_logs import (
    ASSET_TASK,
    TASK_CLOCKS,
    RecoveredLogReview,
    ReviewedDockerHost,
)
from ops.deploy.recovery_sync import SyncEvidenceError, stamp, validate_sync
from tests.deploy.test_docker_host import make_config
from tests.deploy.test_reviewed_recovery import CUTOFF, NOW, fixture


def task_error(task, *, clock="2026-10-01 11:00:00", exception="HTTPError"):
    return (
        f"[{clock},123: ERROR/MainProcess] Task {task}"
        "[aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee] raised unexpected: HTTPError()\n"
        "Traceback (most recent call last):\n"
        '  File "/synthetic/tasks.py", line 8, in update\n'
        "    raise HTTPError()\n"
        f"{exception}\n"
    )


class SyncScheduleTests(unittest.TestCase):
    def test_four_hour_ledger_cadence_passes_between_runs_and_stale_fails_with_details(
        self,
    ):
        _, review, status = fixture()
        for hours, expected in ((3, True), (5, True), (6, False)):
            candidate = deepcopy(status)
            clock = NOW - timedelta(hours=hours)
            candidate["completed_audits"] = [
                {
                    "status": "COMPLETE",
                    "queued_at": (clock - timedelta(minutes=3)).isoformat(),
                    "source_refresh_requested_at": (
                        clock - timedelta(minutes=3)
                    ).isoformat(),
                    "finished_at": (clock + timedelta(minutes=1)).isoformat(),
                    "warning_schema_valid": True,
                    "warning_count": 0,
                    "warning_categories": {},
                    "has_error": False,
                    "price_snapshot_count": 0,
                    "incomplete_price_snapshot_count": 0,
                    "incomplete_price_snapshot_schema_valid": True,
                    "jita_depth_incomplete_price_snapshot_count": 0,
                }
            ]
            candidate["refineries"][0]["ledger_last_update_at"] = clock.isoformat()
            with self.subTest(hours=hours):
                if expected:
                    result = validate_sync(candidate, review, stamp(CUTOFF), now=NOW)
                    self.assertEqual(result["ledger_max_age_seconds"], 18900)
                else:
                    with self.assertRaises(SyncEvidenceError) as caught:
                        validate_sync(candidate, review, stamp(CUTOFF), now=NOW)
                    finding = next(
                        x for x in caught.exception.findings if x["model"] == "refineries"
                    )
                    self.assertEqual(finding["pk"], 10001)
                    self.assertEqual(finding["field"], "ledger_last_update_at")
                    self.assertEqual(finding["maximum_age_seconds"], 18900)
                    self.assertEqual(finding["age_seconds"], 21600)

    def test_jita_depth_warning_proves_reconciliation_but_retains_incomplete_valuation(
        self,
    ):
        _, review, status = fixture()
        audit = status["completed_audits"][0]
        audit.update(
            status="WARNING",
            warning_count=1,
            warning_categories={"jita-depth-insufficient": 1},
            price_snapshot_count=3,
            incomplete_price_snapshot_count=1,
            jita_depth_incomplete_price_snapshot_count=1,
        )

        result = validate_sync(status, review, stamp(CUTOFF), now=NOW)

        self.assertEqual(result["moon_tax_audit_status"], "WARNING")
        self.assertTrue(result["moon_tax_reconciliation_complete"])
        self.assertFalse(result["moon_tax_valuation_complete"])
        self.assertEqual(
            result["moon_tax_warning_categories"], {"jita-depth-insufficient": 1}
        )
        self.assertEqual(result["moon_tax_incomplete_valuation_count"], 1)

    def test_latest_failed_or_unfinished_audit_cannot_fall_back_to_older_success(self):
        _, review, status = fixture()
        for audit_status in ("FAILED", "QUEUED", "REFRESHING", "RUNNING"):
            candidate = deepcopy(status)
            candidate["completed_audits"][0]["status"] = audit_status
            with self.subTest(status=audit_status), self.assertRaises(
                SyncEvidenceError
            ) as caught:
                validate_sync(candidate, review, stamp(CUTOFF), now=NOW)
            self.assertTrue(
                any(
                    finding["reason"] == "latest-audit-not-reconciled"
                    for finding in caught.exception.findings
                )
            )

    def test_malformed_unknown_error_and_mismatched_warning_evidence_blocks(self):
        _, review, status = fixture()
        base = deepcopy(status)
        base["completed_audits"][0].update(
            status="WARNING",
            warning_count=1,
            warning_categories={"jita-depth-insufficient": 1},
            price_snapshot_count=1,
            incomplete_price_snapshot_count=1,
            jita_depth_incomplete_price_snapshot_count=1,
        )
        mutations = (
            ("warning_schema_valid", False),
            ("warning_count", 2),
            ("warning_categories", {"unrecognized": 1}),
            ("has_error", True),
            ("price_snapshot_count", 0),
            ("incomplete_price_snapshot_count", 0),
            ("incomplete_price_snapshot_schema_valid", False),
            ("jita_depth_incomplete_price_snapshot_count", 0),
        )
        for field, value in mutations:
            candidate = deepcopy(base)
            candidate["completed_audits"][0][field] = value
            with self.subTest(field=field), self.assertRaises(SyncEvidenceError):
                validate_sync(candidate, review, stamp(CUTOFF), now=NOW)

    def test_missing_ambiguous_disabled_stale_or_unknown_cadence_never_defaults(self):
        _, review, status = fixture()
        candidates = []
        for field, value in (
            ("moon_tax_config", []),
            ("completed_audits", []),
            ("moon_tax_schedule", []),
            ("memberaudit_schedule", []),
            ("asset_stale_minutes", 0),
            ("asset_stale_minutes", 2000),
        ):
            candidate = deepcopy(status)
            candidate[field] = value
            candidates.append(candidate)
        for key in ("moon_tax_schedule", "memberaudit_schedule"):
            for field, value in (
                ("enabled", False),
                ("interval__period", "days"),
                ("crontab_id", 1),
                ("interval__every", 0),
                ("last_run_at", (NOW - timedelta(hours=3)).isoformat()),
            ):
                candidate = deepcopy(status)
                candidate[key][0][field] = value
                candidates.append(candidate)
            candidate = deepcopy(status)
            candidate[key] *= 2
            candidates.append(candidate)
        for index, candidate in enumerate(candidates):
            with self.subTest(case=index), self.assertRaises(SyncEvidenceError):
                validate_sync(candidate, review, stamp(CUTOFF), now=NOW)

    def test_asset_identity_follows_reregistration_but_missing_or_failed_data_blocks(self):
        _, review, status = fixture()
        self.assertEqual(
            validate_sync(status, review, stamp(CUTOFF), now=NOW)[
                "active_asset_characters"
            ],
            1,
        )
        candidates = []
        for key, field, value in (
            ("asset_characters", "eve_character__character_id", 90000002),
            ("asset_characters", "is_disabled", True),
            ("asset_status", "is_success", False),
            ("asset_status", "has_token_error", True),
            ("asset_status", "run_finished_at", (NOW - timedelta(hours=10)).isoformat()),
        ):
            candidate = deepcopy(status)
            candidate[key][0][field] = value
            candidates.append(candidate)
        candidate = deepcopy(status)
        candidate["asset_status"] = []
        candidates.append(candidate)
        candidate = deepcopy(status)
        candidate["asset_characters"].append(
            {"pk": 5, "is_disabled": True, "eve_character__character_id": 90000002}
        )
        candidates.append(candidate)
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(SyncEvidenceError):
                validate_sync(candidate, review, stamp(CUTOFF), now=NOW)


class RecoveredLogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.host = ReviewedDockerHost(make_config(Path(self.temporary.name)))
        self.host.recovery_baseline_verified = True
        self.host.reviewed_worker_ids = frozenset({"000000000001"})
        _, review, self.status = fixture()
        validate_sync(self.status, review, stamp(CUTOFF), now=NOW)
        self.host.recovered_log_review = RecoveredLogReview(
            self.status, review, started_at=NOW
        )
        self.review = review
        self.service = self.host.config.worker_service
        self.source = self.service + "/000000000001"

    def classify(self, text, *, source=None, phase="rollback"):
        return self.host._classify_log_batch(
            [(source or self.source, text)], {self.service}, phase
        )

    def test_every_reviewed_task_requires_later_success_and_retains_digest(self):
        for task in TASK_CLOCKS:
            text = task_error(task)
            self.assertTrue(self.classify(text))
            self.assertTrue(self.host.recovered_log_review.summary())
            with self.assertRaises(LogScanError):
                self.classify(task_error(task, clock="2026-10-01 11:59:00"))
            with self.assertRaises(LogScanError):
                self.classify(task_error(task, clock="2026-10-01 12:00:00"))
        self.assertEqual(
            sum(x["log_records"] for x in self.host.recovered_log_review.summary()), 4
        )
        self.assertNotIn("synthetic/tasks", str(self.host.recovered_log_review.summary()))

    def test_infrastructure_permissions_other_tasks_and_new_workers_stay_strict(self):
        task = next(iter(TASK_CLOCKS))
        for exception in (
            "PermissionError: denied",
            "HTTPError: 403 Forbidden",
            "HTTPError: 403 Client Error: Forbidden for url: https://example.invalid",
            "HTTPError: <HTTPClientError 401 details=None>",
            "django.db.OperationalError: unavailable",
            "SyntaxError: invalid",
            "ValueError: unexpected",
            "ExceptionGroup: failed",
        ):
            with self.subTest(exception=exception), self.assertRaises(LogScanError):
                self.classify(task_error(task, exception=exception))
        for phase in (None, "candidate", "promoted"):
            with self.subTest(phase=phase), self.assertRaises(LogScanError):
                self.classify(task_error(task), phase=phase)
        with self.assertRaises(LogScanError):
            self.classify(task_error("unrelated.tasks.update"))
        with self.assertRaises(LogScanError):
            self.classify(task_error(ASSET_TASK))  # No identified assets failure.
        with self.assertRaises(LogScanError):
            self.classify(task_error(task), source=self.service + "/000000000002")
        self.host.recovery_baseline_verified = False
        with self.assertRaises(LogScanError):
            self.classify(task_error(task))
        ordinary = DockerHost(self.host.config)
        ordinary.recovery_baseline_verified = True
        with self.assertRaises(LogScanError):
            ordinary._classify_log_batch(
                [(self.source, task_error(task))], {self.service}, "rollback"
            )

    def test_asset_404_uses_stable_identity_and_preserves_other_failures(self):
        text = (
            "[01/Oct/2026 11:00:00] ERROR [memberaudit.models.characters:551] "
            "Synthetic Pilot (ID:5): assets: Error occurred: HTTPClientError: "
            "<HTTPClientError 404 details=None error='Invalid IDs in the request'>\n"
            "Traceback (most recent call last):\n"
            '  File "/synthetic/assets.py", line 1, in update\n'
            "aiopenapi3.errors.HTTPClientError\n\n"
            "The above exception was the direct cause of the following exception:\n\n"
            "Traceback (most recent call last):\n"
            "esi.exceptions.HTTPClientError\n"
        )
        self.assertTrue(self.classify(text))
        self.assertTrue(self.classify(task_error(ASSET_TASK)))
        with self.assertRaises(LogScanError):
            self.classify(task_error(ASSET_TASK))  # Cannot reuse a matched incident.
        for changed in (
            text.replace("ID:5", "ID:6"),
            text.replace("404", "403"),
            text.replace("11:00:00", "11:59:00"),
            text + "Unrelated ERROR\n",
        ):
            with self.assertRaises(LogScanError):
                self.classify(changed)

    def test_debug_claim_values_are_not_severity_and_unknown_metadata_is_redacted(self):
        prefix = "[01/Oct/2026 12:01:00] DEBUG [esi.managers:162] "
        self.assertTrue(
            self.classify(
                prefix
                + repr(
                    {"sub": "CHARACTER:EVE:90000001", "scp": [], "name": "ERROR Synthetic"}
                )
            )
        )
        with self.assertRaises(LogScanError) as caught:
            self.classify(
                prefix
                + repr(
                    {
                        "sub": "CHARACTER:EVE:90000001",
                        "scp": [],
                        "unknown": "ERROR private-claim",
                    }
                )
                + "\n[2026-10-01 12:02:00: ERROR/MainProcess] unrelated failure\n"
            )
        self.assertNotIn("private-claim", str(caught.exception))
        self.assertIn("unrelated failure", str(caught.exception))

    def test_discord_history_is_reported_as_warning_and_fresh_or_denied_requests_block(
        self,
    ):
        self.service = "allianceauth_worker_services"
        self.source = self.service + "/000000000001"
        template = (
            "[01/Oct/2026 11:00:00] ERROR "
            "[allianceauth.services.modules.discord.discord_client.client:679] "
            "[Discord Service] " + "a" * 32 + ": Discord API returned error code 429 "
            'for member ID 900000001 with this response: {"message":"Rate limited","retry_after":60}'
        )
        self.assertTrue(self.classify(template))
        self.assertTrue(self.classify(template + "\n\n"))
        self.assertTrue(self.classify(template + "\n.\n"))
        self.assertEqual(
            self.host.recovered_log_review.summary()[0]["category"],
            "historical-Discord-rate-limit",
        )
        for text in (
            template.replace("429", "403"),
            template.replace("11:00:00", "12:00:00"),
            template.replace('"retry_after":60', '"code":50013'),
        ):
            with self.assertRaises(LogScanError):
                self.classify(text)

    def test_full_stream_batches_keep_accepted_findings_and_unrelated_tail_error(self):
        text = (
            "[2026-10-01 10:00:00: INFO/MainProcess] normal\n" * 10000
            + task_error(next(iter(TASK_CLOCKS)))
            + "[2026-10-01 11:00:00: ERROR/MainProcess] unrelated final error\n"
        )
        failures = []
        for batch in self.host._log_batches(
            iter(text.splitlines()), owner_scoped=True, deadline=time.monotonic() + 30
        ):
            try:
                self.classify(batch)
            except LogScanError as error:
                failures.extend(error.findings)
        self.assertTrue(any("unrelated final error" in x for x in failures))
        self.assertEqual(
            sum(x["log_records"] for x in self.host.recovered_log_review.summary()), 1
        )

    def test_completion_demands_a_new_sync_check(self):
        with self.assertRaises(DeploymentError):
            self.host._verify_restored(None, set())

        def stale():
            raise DeploymentError("current sync became stale")

        self.host.recheck_recovered_sync = stale
        with self.assertRaisesRegex(DeploymentError, "became stale"):
            self.host._verify_restored(None, set())
