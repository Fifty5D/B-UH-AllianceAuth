from __future__ import annotations

import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.deploy.contracts import (
    DeploymentError,
    DeploymentRequest,
    ValidatedBundle,
)
from ops.deploy.engine import (
    DEPLOYMENT_STATES,
    DeploymentEngine,
    DeploymentJournal,
    PreflightEngine,
    PreflightJournal,
)


def make_bundle(root: Path, *, attempt: int = 1) -> ValidatedBundle:
    request = DeploymentRequest(
        mode="deploy",
        repository="Fifty5D/B-UH-AllianceAuth",
        release_commit="a" * 40,
        release_ref="release/platform-v0.4.0",
        platform_version="0.4.0",
        manifest_sha256="b" * 64,
        workflow_run_id="123456789",
        workflow_run_attempt=attempt,
    )
    return ValidatedBundle(
        root=root,
        release_dir=root / "release",
        request=request,
        manifest={"source_commit": "c" * 40},
        install_plan={},
    )


class FakeBackend:
    def __init__(self, fail_at: str | None = None):
        self.fail_at = fail_at
        self.calls: list[str] = []
        self.verification = None

    def _call(self, name):
        self.calls.append(name)
        if self.fail_at == name:
            raise DeploymentError(f"synthetic {name} failure")

    def validate(self, bundle):
        self._call("validate")

    def prepare_candidate(self, bundle):
        self._call("prepare_candidate")

    def backup(self, bundle):
        self._call("backup")
        return {"sha256": "d" * 64, "filename": "database.sql.gz"}

    def migrate(self, bundle):
        self._call("migrate")

    def candidate_health(self, bundle):
        self._call("candidate_health")

    def switch_traffic(self, bundle):
        self._call("switch_traffic")

    def replace_workers(self, bundle):
        self._call("replace_workers")

    def stabilize(self, bundle):
        self._call("stabilize")
        self.verification = {
            "filename": "HEALTH.json",
            "sha256": "e" * 64,
            "result": "success",
            "iterations": 21,
            "allowed_log_findings": 0,
            "warnings": [],
            "failure": None,
        }
        return self.verification

    def stabilization_evidence(self):
        return self.verification

    def promote_web(self, bundle):
        self._call("promote_web")

    def finalize(self, bundle):
        self._call("finalize")

    def cleanup_success(self, bundle):
        self._call("cleanup_success")
        return "cleanup passed"

    def rollback(self, bundle, last_state):
        self.calls.append(f"rollback:{last_state}")
        return f"recovered from {last_state}"


class DoubleFailureBackend(FakeBackend):
    def prepare_candidate(self, bundle):
        self.calls.append("prepare_candidate")
        raise DeploymentError("synthetic candidate failure")

    def rollback(self, bundle, last_state):
        self.calls.append(f"rollback:{last_state}")
        raise DeploymentError("synthetic cleanup failure")


class HealthFailureBackend(FakeBackend):
    def stabilize(self, bundle):
        self.calls.append("stabilize")
        self.verification = {
            "filename": "HEALTH.json",
            "sha256": "f" * 64,
            "result": "failed",
            "iterations": 2,
            "allowed_log_findings": 1,
            "warnings": [
                'ERROR expected probe {"access_token":"warning-secret"}'
            ],
            "failure": "CRITICAL synthetic health failure ?token=failure-secret",
        }
        raise DeploymentError("synthetic stabilization failure")


class DeploymentEngineTests(unittest.TestCase):
    def test_catchable_termination_signals_trigger_guarded_rollback(self):
        signal_values = tuple(
            value
            for name in ("SIGHUP", "SIGTERM")
            if (value := getattr(signal, name, None)) is not None
        )
        for operation in ("deploy", "preflight"):
            for signal_value in signal_values:
                with (
                    self.subTest(operation=operation, signal=signal_value),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary)
                    bundle = make_bundle(root)
                    handlers = {}
                    restored = {}

                    def set_signal(number, handler):
                        if callable(handler):
                            handlers[number] = handler
                        else:
                            restored[number] = handler

                    class InterruptingBackend(FakeBackend):
                        def _interrupt(self):
                            handlers[signal_value](signal_value, None)

                        def prepare_candidate(self, candidate_bundle):
                            super().prepare_candidate(candidate_bundle)
                            if operation == "preflight":
                                self._interrupt()

                        def switch_traffic(self, candidate_bundle):
                            super().switch_traffic(candidate_bundle)
                            if operation == "deploy":
                                self._interrupt()

                    backend = InterruptingBackend()
                    with mock.patch(
                        "ops.deploy.engine.signal.getsignal",
                        return_value="prior-handler",
                    ), mock.patch(
                        "ops.deploy.engine.signal.signal", side_effect=set_signal
                    ):
                        if operation == "deploy":
                            runner = DeploymentEngine(
                                backend, DeploymentJournal(root / "state", bundle)
                            )
                        else:
                            runner = PreflightEngine(
                                backend, PreflightJournal(root / "state", bundle)
                            )
                        with self.assertRaisesRegex(
                            DeploymentError, "automatic rollback"
                        ):
                            runner.run(bundle)

                    self.assertTrue(backend.calls[-1].startswith("rollback:"))
                    self.assertEqual(set(restored), set(handlers))

    def test_signal_after_backend_finalize_rolls_back_before_success_is_published(self):
        signal_value = getattr(signal, "SIGTERM", None)
        if signal_value is None:  # pragma: no cover - supported deployment hosts
            self.skipTest("SIGTERM is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = make_bundle(root)
            handlers = {}

            def set_signal(number, handler):
                if callable(handler):
                    handlers[number] = handler

            class FinalizeInterruptBackend(FakeBackend):
                def finalize(self, candidate_bundle):
                    super().finalize(candidate_bundle)
                    handlers[signal_value](signal_value, None)

            backend = FinalizeInterruptBackend()
            journal = DeploymentJournal(root / "state", bundle)
            with mock.patch(
                "ops.deploy.engine.signal.getsignal", return_value="prior-handler"
            ), mock.patch(
                "ops.deploy.engine.signal.signal", side_effect=set_signal
            ), self.assertRaisesRegex(DeploymentError, "automatic rollback"):
                DeploymentEngine(backend, journal).run(bundle)

            self.assertEqual(backend.calls[-1], "rollback:web_promoted")
            self.assertFalse((root / "state/current.json").exists())
            self.assertEqual(json.loads(journal.path.read_text())["result"], "failed")

    def test_preflight_builds_candidate_then_restores_without_mutation_steps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = make_bundle(root)
            journal = PreflightJournal(root / "state", bundle)
            backend = FakeBackend()
            PreflightEngine(backend, journal).run(bundle)

            self.assertEqual(
                backend.calls,
                ["validate", "prepare_candidate", "rollback:validated"],
            )
            record = json.loads(journal.path.read_text())
            self.assertEqual(record["operation"], "preflight")
            self.assertEqual(record["state"], "candidate_validated")
            self.assertEqual(record["result"], "success")
            self.assertFalse((root / "state/current.json").exists())

    def test_preflight_build_failure_is_retained_and_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = make_bundle(root)
            journal = PreflightJournal(root / "state", bundle)
            backend = FakeBackend("prepare_candidate")
            with self.assertRaisesRegex(DeploymentError, "prepare_candidate"):
                PreflightEngine(backend, journal).run(bundle)

            self.assertEqual(
                backend.calls,
                ["validate", "prepare_candidate", "rollback:validated"],
            )
            record = json.loads(journal.path.read_text())
            self.assertEqual(record["result"], "failed")
            self.assertIn("synthetic prepare_candidate failure", record["failure_detail"])

    def test_preflight_records_the_cleanup_failure_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = make_bundle(root)
            journal = PreflightJournal(root / "state", bundle)
            backend = DoubleFailureBackend()
            with self.assertRaisesRegex(DeploymentError, "candidate failure"):
                PreflightEngine(backend, journal).run(bundle)

            record = json.loads(journal.path.read_text())
            self.assertIn("synthetic cleanup failure", record["recovery"])
            self.assertEqual(record["rollback"]["result"], "failed")

    def test_success_records_every_state_before_publishing_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = make_bundle(root)
            journal = DeploymentJournal(root / "state", bundle)
            backend = FakeBackend()
            DeploymentEngine(backend, journal).run(bundle)

            attempt = json.loads(journal.path.read_text())
            self.assertEqual(
                [entry["state"] for entry in attempt["history"]],
                list(DEPLOYMENT_STATES),
            )
            self.assertEqual(attempt["result"], "success")
            current = json.loads((root / "state/current.json").read_text())
            self.assertEqual(current["release_commit"], "a" * 40)
            self.assertEqual(current["manifest_sha256"], "b" * 64)
            self.assertEqual(backend.calls[-1], "cleanup_success")
            self.assertEqual(attempt["verification"]["iterations"], 21)
            self.assertEqual(attempt["cleanup"]["result"], "passed")

    def test_each_failure_rolls_back_and_never_publishes_current(self):
        steps = (
            "validate",
            "prepare_candidate",
            "backup",
            "migrate",
            "candidate_health",
            "switch_traffic",
            "replace_workers",
            "stabilize",
            "promote_web",
            "finalize",
        )
        for attempt, failed_step in enumerate(steps, start=1):
            with self.subTest(step=failed_step), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                bundle = make_bundle(root, attempt=attempt)
                journal = DeploymentJournal(root / "state", bundle)
                backend = FakeBackend(failed_step)
                with self.assertRaisesRegex(DeploymentError, failed_step):
                    DeploymentEngine(backend, journal).run(bundle)
                record = json.loads(journal.path.read_text())
                self.assertEqual(record["result"], "failed")
                self.assertIn(f"synthetic {failed_step} failure", record["failure_detail"])
                self.assertIn("recovered", record["recovery"])
                self.assertEqual(record["rollback"]["result"], "passed")
                self.assertTrue(backend.calls[-1].startswith("rollback:"))
                self.assertFalse((root / "state/current.json").exists())

    def test_journal_failure_after_finalize_still_enters_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = make_bundle(root)

            class FailingVerifiedJournal(DeploymentJournal):
                def advance(self, state, **kwargs):
                    super().advance(state, **kwargs)
                    if state == "verified":
                        raise DeploymentError("synthetic verified journal failure")

            journal = FailingVerifiedJournal(root / "state", bundle)
            backend = FakeBackend()
            with self.assertRaisesRegex(DeploymentError, "journal failure"):
                DeploymentEngine(backend, journal).run(bundle)

            self.assertEqual(backend.calls[-1], "rollback:verified")
            record = json.loads(journal.path.read_text())
            self.assertEqual(record["result"], "failed")
            self.assertEqual(record["rollback"]["result"], "passed")
            self.assertFalse((root / "state/current.json").exists())

    def test_attempt_replay_and_invalid_transition_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = make_bundle(root)
            journal = DeploymentJournal(root / "state", bundle)
            with self.assertRaisesRegex(DeploymentError, "already received"):
                DeploymentJournal(root / "state", bundle)
            with self.assertRaisesRegex(DeploymentError, "transition"):
                journal.advance("backed_up")
            for state in DEPLOYMENT_STATES:
                if state == "stabilized":
                    journal.advance(
                        state,
                        verification={
                            "filename": "HEALTH.json",
                            "sha256": "e" * 64,
                            "result": "success",
                            "iterations": 1,
                            "allowed_log_findings": 0,
                            "warnings": [],
                            "failure": None,
                        },
                    )
                else:
                    journal.advance(state)
            with self.assertRaisesRegex(DeploymentError, "no further state"):
                journal.advance("verified")

    def test_failure_details_are_bounded_and_secret_redacted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = make_bundle(root)
            journal = DeploymentJournal(root / "state", bundle)
            journal.fail(
                DeploymentError(
                    'traffic switch failed {"access_token":"json-secret"} '
                    "?token=query-secret Authorization: Bearer bearer-secret "
                    "Cookie: sessionid=cookie-secret; "
                    "Set-Cookie: csrftoken=csrf-secret; Bot abcdefghijklmnop "
                    "https://discord.com/api/webhooks/123/webhook-secret "
                    "https://deploy-user:deploy-pass@example.invalid/path "
                    "eyJabcdefghijk.abcdefghijk.abcdefghijk"
                ),
                "production unchanged",
                rollback_passed=True,
            )
            record = json.loads(journal.path.read_text())
            for secret in (
                "json-secret",
                "query-secret",
                "bearer-secret",
                "cookie-secret",
                "csrf-secret",
                "abcdefghijklmnop",
                "webhook-secret",
                "deploy-pass",
                "eyJabcdefghijk",
            ):
                self.assertNotIn(secret, record["failure_detail"])
                self.assertNotIn(secret, record["failure"])
            self.assertIn("<redacted>", record["failure_detail"])

    def test_failed_stabilization_evidence_and_rollback_result_are_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = make_bundle(root)
            journal = DeploymentJournal(root / "state", bundle)
            backend = HealthFailureBackend()
            with self.assertRaisesRegex(DeploymentError, "stabilization"):
                DeploymentEngine(backend, journal).run(bundle)

            record = json.loads(journal.path.read_text())
            self.assertEqual(record["verification"]["result"], "failed")
            self.assertEqual(len(record["verification"]["warnings"]), 1)
            self.assertIn(
                "<redacted>", record["verification"]["warnings"][0]
            )
            self.assertIn("CRITICAL synthetic", record["verification"]["failure"])
            self.assertNotIn("warning-secret", record["verification"]["warnings"][0])
            self.assertNotIn("failure-secret", record["verification"]["failure"])
            self.assertEqual(record["rollback"]["result"], "passed")

    def test_stabilization_evidence_is_required_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = DeploymentJournal(root / "state", make_bundle(root))
            for state in DEPLOYMENT_STATES[: DEPLOYMENT_STATES.index("stabilized")]:
                journal.advance(state)
            with self.assertRaisesRegex(DeploymentError, "requires bounded"):
                journal.advance("stabilized")
            self.assertEqual(journal.state, "workers_replaced")

    def test_post_success_cleanup_failure_never_rolls_back_verified_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = make_bundle(root)
            journal = DeploymentJournal(root / "state", bundle)
            backend = FakeBackend("cleanup_success")
            DeploymentEngine(backend, journal).run(bundle)

            record = json.loads(journal.path.read_text())
            self.assertEqual(record["result"], "success")
            self.assertEqual(record["cleanup"]["result"], "failed")
            self.assertNotIn("rollback:verified", backend.calls)
            self.assertTrue((root / "state/current.json").is_file())


if __name__ == "__main__":
    unittest.main()
