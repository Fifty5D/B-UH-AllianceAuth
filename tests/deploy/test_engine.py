from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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

    def swap(self, bundle):
        self._call("swap")

    def health(self, bundle):
        self._call("health")

    def finalize(self, bundle):
        self._call("finalize")

    def rollback(self, bundle, last_state):
        self.calls.append(f"rollback:{last_state}")
        return f"recovered from {last_state}"


class DeploymentEngineTests(unittest.TestCase):
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
            self.assertEqual(backend.calls[-1], "finalize")

    def test_each_failure_rolls_back_and_never_publishes_current(self):
        steps = (
            "validate",
            "prepare_candidate",
            "backup",
            "migrate",
            "swap",
            "health",
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
                self.assertTrue(backend.calls[-1].startswith("rollback:"))
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
                journal.advance(state)
            with self.assertRaisesRegex(DeploymentError, "no further state"):
                journal.advance("verified")


if __name__ == "__main__":
    unittest.main()
