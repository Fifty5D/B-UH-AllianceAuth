"""Execution tests for the one bounded published-v0.6.2 validation recovery."""

from __future__ import annotations

import copy
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ops.release import validation_recovery as recovery


ROOT = Path(__file__).resolve().parents[2]


def run(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed: {completed.stderr[-1200:]}"
        )
    return completed.stdout.strip()


class RecoveryRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        run(root, "init", "--quiet", "--initial-branch=main")
        run(root, "config", "user.name", "Recovery Fixture")
        run(root, "config", "user.email", "recovery-fixture@example.invalid")
        run(root, "config", "core.autocrlf", "false")

        fixture_paths = sorted(
            set(recovery.ACTIVATION_PATHS)
            | set(recovery.CONTINUATION_PATHS)
            | set(recovery.REPAIR_PATHS)
            | set(recovery.HARNESS_PATHS)
            | set(recovery.TEST_SUPPORT_PATHS)
        )
        for relative in fixture_paths:
            path = root.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"source:{relative}\n", encoding="utf-8")
        runtime = root / "application-runtime.bin"
        runtime.write_bytes(b"unchanged-application-runtime\n")
        run(root, "add", "--all")
        run(root, "commit", "--quiet", "-m", "reviewed source")
        self.source = run(root, "rev-parse", "HEAD")
        self.source_tree = run(root, "rev-parse", "HEAD^{tree}")

        run(root, "checkout", "--quiet", "-b", "published-release")
        manifest = {
            "platform_version": "0.6.2",
            "source_commit": self.source,
        }
        manifest_bytes = recovery.canonical_json_bytes(manifest)
        manifest_path = root / "releases/platform/v0.6.2/RELEASE.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_bytes(manifest_bytes)
        run(root, "add", "--all")
        run(root, "commit", "--quiet", "-m", "published release")
        self.release = run(root, "rev-parse", "HEAD")
        self.release_tree = run(root, "rev-parse", "HEAD^{tree}")
        self.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

        run(root, "checkout", "--quiet", "-b", "recovery-feature", self.source)
        for relative in recovery.ACTIVATION_PATHS:
            path = root.joinpath(*relative.split("/"))
            path.write_text(f"activation:{relative}\n", encoding="utf-8")
        run(root, "add", "--all")
        run(root, "commit", "--quiet", "-m", "bounded recovery")
        self.feature = run(root, "rev-parse", "HEAD")
        self.activation_tree = run(root, "rev-parse", "HEAD^{tree}")

        run(root, "checkout", "--quiet", "-B", "main", self.source)
        run(root, "merge", "--quiet", "--no-ff", "recovery-feature", "-m", "activate")
        self.activation = run(root, "rev-parse", "HEAD")

        run(root, "checkout", "--quiet", "-b", "recovery-continuation", self.activation)
        for relative in recovery.CONTINUATION_PATHS:
            path = root.joinpath(*relative.split("/"))
            path.write_text(f"continuation:{relative}\n", encoding="utf-8")
        run(root, "add", "--all")
        run(root, "commit", "--quiet", "-m", "bounded recovery continuation")
        self.continuation_feature = run(root, "rev-parse", "HEAD")
        self.continuation_tree = run(root, "rev-parse", "HEAD^{tree}")
        run(root, "checkout", "--quiet", "-B", "main", self.activation)
        run(
            root,
            "merge",
            "--quiet",
            "--no-ff",
            "recovery-continuation",
            "-m",
            "continue recovery",
        )
        self.continuation = run(root, "rev-parse", "HEAD")

        run(root, "checkout", "--quiet", "-b", "recovery-digest-repair", self.continuation)
        for relative in recovery.REPAIR_PATHS:
            path = root.joinpath(*relative.split("/"))
            path.write_text(f"repair:{relative}\n", encoding="utf-8")
        run(root, "add", "--all")
        run(root, "commit", "--quiet", "-m", "bounded digest repair")
        self.repair_feature = run(root, "rev-parse", "HEAD")
        self.repair_tree = run(root, "rev-parse", "HEAD^{tree}")
        run(root, "checkout", "--quiet", "-B", "main", self.continuation)
        run(
            root,
            "merge",
            "--quiet",
            "--no-ff",
            "recovery-digest-repair",
            "-m",
            "repair recovery digest handoff",
        )
        self.repair = run(root, "rev-parse", "HEAD")

        self.contract = {
            "activation": {
                "allowed_paths": list(recovery.ACTIVATION_PATHS),
                "base_commit": self.source,
                "commit": self.activation,
                "feature_head": self.feature,
                "pull_request": 53,
                "tree": self.activation_tree,
                "validation": {"run_attempt": 1, "run_id": 100},
            },
            "continuation": {
                "allowed_paths": list(recovery.CONTINUATION_PATHS),
                "base_commit": self.activation,
                "commit": self.continuation,
                "feature_head": self.continuation_feature,
                "pull_request": 54,
                "tree": self.continuation_tree,
                "validation": {"run_attempt": 1, "run_id": 106},
            },
            "repair": {
                "allowed_paths": list(recovery.REPAIR_PATHS),
                "base_commit": self.continuation,
                "harness_paths": list(recovery.HARNESS_PATHS),
                "pull_request": 55,
                "test_support_paths": list(recovery.TEST_SUPPORT_PATHS),
                "test_support_root": recovery.TEST_SUPPORT_ROOT,
            },
            "failed_publication": {
                "artifact": {
                    "digest": "sha256:" + "8" * 64,
                    "id": 112,
                    "name": "failed-publication",
                },
                "jobs": [],
                "run_attempt": 1,
                "run_id": 111,
            },
            "failed_validation": {
                "artifact": {"digest": "sha256:" + "9" * 64, "id": 110, "name": "failed"},
                "jobs": [],
                "run_attempt": 1,
                "run_id": 109,
            },
            "feature": {"head_commit": "1" * 40, "pull_request": 51},
            "main_validation": {"run_attempt": 1, "run_id": 101},
            "platform_version": "0.6.2",
            "preflight": {
                "artifact_digest": "sha256:" + "2" * 64,
                "artifact_id": 102,
                "artifact_name": "platform-v2-preflight-103-1",
                "failed_job": {"id": 105, "name": "Publish approval"},
                "jobs": [],
                "passed_job": {"id": 104, "name": "Preflight"},
                "run_attempt": 1,
                "run_id": 103,
            },
            "recovery_id": recovery.RECOVERY_ID,
            "release": {
                "commit": self.release,
                "manifest_sha256": self.manifest_sha256,
                "ref": "release/platform-v0.6.2",
                "source_commit": self.source,
                "source_tree": self.source_tree,
                "tree": self.release_tree,
            },
            "repository": "Fifty5D/B-UH-AllianceAuth",
            "schema_version": recovery.SCHEMA_VERSION,
            "sync": {"branch": "sync/platform-v0.6.2", "pull_request": 52},
        }

    def add_sync_merge(self) -> str:
        run(
            self.root,
            "checkout",
            "--quiet",
            "-B",
            "continued-main",
            self.repair,
        )
        run(
            self.root,
            "merge",
            "--quiet",
            "--no-ff",
            "published-release",
            "-m",
            "synchronize release",
        )
        return run(self.root, "rev-parse", "HEAD")


class PublishedReleaseRecoveryTests(unittest.TestCase):
    def test_repository_contract_is_canonical_and_pinned(self) -> None:
        contract = recovery.load_contract(
            ROOT / "ops/release/published-release-recovery-v0.6.2.json"
        )
        self.assertEqual(contract["release"]["commit"], "6074b965cbd2e6ab2630cd539ee455b8d419aef6")
        self.assertEqual(contract["activation"]["pull_request"], 53)
        self.assertEqual(contract["continuation"]["pull_request"], 54)
        self.assertEqual(
            contract["continuation"]["base_commit"],
            "e0bd37fafcedee3135aa4c4d6bdfc7778d032d48",
        )
        self.assertEqual(
            contract["required_check"]["context"], recovery.REQUIRED_CHECK_CONTEXT
        )
        self.assertEqual(
            contract["required_check"]["app_id"], recovery.REQUIRED_CHECK_APP_ID
        )
        self.assertEqual(
            contract["required_check"]["historical_failure"]["check_run_id"],
            102298823162,
        )
        self.assertEqual(
            contract["activation"]["allowed_paths"],
            list(recovery.ACTIVATION_PATHS),
        )

    def test_activation_continuation_repair_and_hold_have_exact_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RecoveryRepository(Path(temporary) / "repository")
            activation = recovery.verify_activation(
                fixture.root,
                contract=fixture.contract,
            )
            self.assertEqual(activation["activation_commit"], fixture.activation)
            self.assertEqual(activation["activation_tree"], fixture.activation_tree)

            continuation = recovery.verify_continuation(
                fixture.root,
                contract=fixture.contract,
            )
            self.assertEqual(continuation["continuation_commit"], fixture.continuation)
            self.assertEqual(continuation["feature_head"], fixture.continuation_feature)

            feature = recovery.validate_repair(
                fixture.root,
                fixture.repair_feature,
                event_name="pull_request",
                pull_request=55,
                contract=fixture.contract,
            )
            self.assertIsNone(feature["repair_commit"])
            self.assertEqual(feature["feature_head"], fixture.repair_feature)

            repair = recovery.validate_repair(
                fixture.root,
                fixture.repair,
                event_name="workflow_dispatch",
                pull_request=None,
                contract=fixture.contract,
            )
            self.assertEqual(
                repair["repair_commit"], fixture.repair
            )
            self.assertEqual(
                repair["repair_tree"], fixture.repair_tree
            )
            self.assertEqual(
                [item["path"] for item in repair["paths"]],
                list(recovery.REPAIR_PATHS),
            )
            held = recovery.validate_hold(
                fixture.root, fixture.repair, contract=fixture.contract
            )
            self.assertEqual(held["state"], "repair-pending-sync")

            synchronized = fixture.add_sync_merge()
            held = recovery.validate_hold(
                fixture.root, synchronized, contract=fixture.contract
            )
            self.assertEqual(held["state"], "synchronized-awaiting-retirement")
            self.assertEqual(held["repair_commit"], fixture.repair)

    def test_unreviewed_activation_path_and_dirty_harness_target_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RecoveryRepository(Path(temporary) / "repository")
            run(
                fixture.root,
                "checkout",
                "--quiet",
                "-b",
                "unsafe",
                fixture.repair_feature,
            )
            (fixture.root / "unreviewed-runtime.py").write_text(
                "changed = True\n", encoding="utf-8"
            )
            run(fixture.root, "add", "--all")
            run(fixture.root, "commit", "--quiet", "-m", "unreviewed change")
            unsafe = run(fixture.root, "rev-parse", "HEAD")
            with self.assertRaisesRegex(
                recovery.ValidationRecoveryError, "outside the reviewed scope"
            ):
                recovery.validate_repair(
                    fixture.root,
                    unsafe,
                    event_name="pull_request",
                    pull_request=55,
                    contract=fixture.contract,
                )

            target = Path(temporary) / "published"
            run(fixture.root, "worktree", "add", "--quiet", "--detach", str(target), fixture.release)
            (target / "untracked.txt").write_text("unexpected\n", encoding="utf-8")
            with self.assertRaisesRegex(
                recovery.ValidationRecoveryError, "not clean"
            ):
                recovery.apply_harness(
                    target, fixture.repair, contract=fixture.contract
                )

    def test_harness_stages_only_declared_support_and_preserves_release_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RecoveryRepository(Path(temporary) / "repository")
            target = Path(temporary) / "published"
            run(fixture.root, "worktree", "add", "--quiet", "--detach", str(target), fixture.release)
            runtime = (target / "application-runtime.bin").read_bytes()
            manifest = (target / "releases/platform/v0.6.2/RELEASE.json").read_bytes()
            report = recovery.apply_harness(
                target, fixture.repair, contract=fixture.contract
            )
            changed = run(target, "diff", "--name-only").splitlines()
            self.assertEqual(changed, list(recovery.HARNESS_PATHS))
            self.assertEqual(
                [item["path"] for item in report["files"]],
                list(recovery.HARNESS_PATHS),
            )
            self.assertEqual(
                [item["path"] for item in report["test_support"]["files"]],
                list(recovery.TEST_SUPPORT_PATHS),
            )
            self.assertEqual(
                run(target, "ls-files", "--others", "--exclude-standard").splitlines(),
                [
                    f"{recovery.TEST_SUPPORT_ROOT}/{path}"
                    for path in recovery.TEST_SUPPORT_PATHS
                ],
            )
            self.assertEqual((target / "application-runtime.bin").read_bytes(), runtime)
            self.assertEqual(
                (target / "releases/platform/v0.6.2/RELEASE.json").read_bytes(),
                manifest,
            )
            self.assertEqual(report["release_commit"], fixture.release)
            self.assertEqual(report["release_tree"], fixture.release_tree)

    def test_attestation_separates_release_harness_and_workflow_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = RecoveryRepository(Path(temporary) / "repository")
            attestation = recovery.create_attestation(
                fixture.root,
                fixture.repair,
                repository=fixture.contract["repository"],
                run_id=9001,
                run_attempt=1,
                actor="Fifty5D",
                contract=fixture.contract,
            )
            self.assertEqual(attestation["release"]["commit"], fixture.release)
            self.assertEqual(attestation["activation"]["activation_commit"], fixture.activation)
            self.assertEqual(attestation["harness"]["commit"], fixture.repair)
            self.assertNotEqual(
                attestation["release"]["tree"], attestation["harness"]["tree"]
            )
            self.assertEqual(attestation["validation"]["workflow_path"], recovery.WORKFLOW_PATH)
            self.assertEqual(
                recovery.validate_attestation_data(
                    attestation,
                    contract=fixture.contract,
                    expected_run_id=9001,
                    expected_run_attempt=1,
                    expected_repair=fixture.repair,
                ),
                attestation,
            )

            altered = copy.deepcopy(attestation)
            altered["release"]["tree"] = "f" * 40
            with self.assertRaisesRegex(
                recovery.ValidationRecoveryError, "identity changed"
            ):
                recovery.validate_attestation_data(
                    altered, contract=fixture.contract
                )

    def test_actual_pending_ledger_rehearses_future_v062_sync(self) -> None:
        """Exercise the bounded fallback with the real immutable release ledger."""

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            base = Path(temporary)
            mirror = base / "origin.git"
            run(base, "clone", "--quiet", "--mirror", str(ROOT), str(mirror))
            run(mirror, "config", "user.name", "Recovery Ledger Fixture")
            run(
                mirror,
                "config",
                "user.email",
                "recovery-ledger@example.invalid",
            )
            release_refs = run(
                ROOT,
                "for-each-ref",
                "--format=%(refname:strip=3) %(objectname)",
                "refs/remotes/origin/release/platform-v*",
            ).splitlines()
            self.assertTrue(release_refs)
            for record in release_refs:
                branch, commit = record.split()
                run(mirror, "update-ref", f"refs/heads/{branch}", commit)

            contract = recovery.load_contract(
                ROOT / "ops/release/published-release-recovery-v0.6.2.json"
            )
            review = base / "review"
            run(base, "clone", "--quiet", str(mirror), str(review))
            run(review, "config", "user.name", "Recovery Repair Fixture")
            run(
                review,
                "config",
                "user.email",
                "recovery-repair@example.invalid",
            )
            run(
                review,
                "checkout",
                "--quiet",
                "-b",
                "recovery-repair",
                contract["repair"]["base_commit"],
            )
            for relative in contract["repair"]["allowed_paths"]:
                destination = review / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, destination)
            run(review, "add", "--all")
            run(review, "commit", "--quiet", "-m", "reviewed recovery repair")
            feature = run(review, "rev-parse", "HEAD")
            run(review, "push", "--quiet", "origin", "recovery-repair")
            feature_tree = run(mirror, "rev-parse", f"{feature}^{{tree}}")
            repair = run(
                mirror,
                "commit-tree",
                feature_tree,
                "-p",
                contract["repair"]["base_commit"],
                "-p",
                feature,
                "-m",
                "synthetic exact digest repair",
            )
            run(
                mirror,
                "update-ref",
                "refs/heads/recovery-digest-repair",
                repair,
            )

            checkout = base / "checkout"
            run(
                base,
                "clone",
                "--quiet",
                "--no-checkout",
                str(mirror),
                str(checkout),
            )
            run(checkout, "config", "core.autocrlf", "false")
            run(checkout, "checkout", "--quiet", "--detach", repair)
            report = recovery.verify_pending_ledger(
                checkout,
                repair,
                event_name="push",
                pull_request=None,
                contract=contract,
                environ={**os.environ, "GITHUB_TOKEN": "synthetic-token"},
            )
            self.assertEqual(
                report["latest_release"],
                {
                    "manifest_sha256": contract["release"]["manifest_sha256"],
                    "platform_version": "0.6.2",
                    "release_commit": contract["release"]["commit"],
                },
            )
            self.assertEqual(
                report["next_release"],
                {
                    "deployment_predecessor": None,
                    "platform_version": "0.6.3",
                    "previous_platform_version": "0.6.2",
                    "release_required": True,
                },
            )
            self.assertEqual(
                report["synthetic_merge_tree"],
                recovery._merge_tree(
                    checkout, repair, contract["release"]["commit"]
                ),
            )


if __name__ == "__main__":
    unittest.main()
