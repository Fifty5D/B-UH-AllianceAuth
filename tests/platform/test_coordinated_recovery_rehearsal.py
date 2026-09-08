"""Execution rehearsal for the one bounded v0.5.6 release-gap recovery."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from ops.deploy import contracts, receiver_upgrade, request_archive
from ops.deploy.docker_host import DockerHost
from ops.deploy.engine import (
    DeploymentEngine,
    DeploymentJournal,
    PreflightEngine,
    PreflightJournal,
)
from ops.release import buh_release, recovery_policy
from tests.release import test_platform_approval as approval_fixture


ROOT = Path(__file__).resolve().parents[2]
V061_RELEASE = "43234a8c0b6371fdfc62b59fa75a7980924ac3a5"


def run(*arguments: object, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(argument) for argument in arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def populate_receiver(root: Path, marker: str) -> None:
    for index, logical in enumerate(receiver_upgrade.TARGETS):
        path = root / logical.removeprefix("/")
        if logical in {"/usr/local/lib/buh-platform-v2", "/etc/buh-platform-v2"}:
            path.mkdir(parents=True)
            (path / "state.txt").write_text(
                f"{marker}:{index}\n", encoding="utf-8"
            )
            path.chmod(0o750)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{marker}:{index}\n", encoding="utf-8")
            path.chmod(0o755)


def receiver_fingerprint(root: Path) -> dict[str, tuple[tuple[str, bytes], ...]]:
    result: dict[str, tuple[tuple[str, bytes], ...]] = {}
    for logical in receiver_upgrade.TARGETS:
        path = root / logical.removeprefix("/")
        files = [path] if path.is_file() else sorted(
            (item for item in path.rglob("*") if item.is_file()),
            key=lambda item: item.as_posix(),
        )
        result[logical] = tuple(
            (item.relative_to(path).as_posix() if item != path else ".", item.read_bytes())
            for item in files
        )
    return result


class SyntheticFleet:
    """Stateful external boundary used by the real deployment engines."""

    def __init__(self, bundle: contracts.ValidatedBundle, *, fail_at: str | None = None):
        policy = recovery_policy.load_policy()
        self.bundle = bundle
        self.fail_at = fail_at
        self.calls: list[str] = []
        self.original_images = {
            service: identity["image_id"]
            for service, identity in policy["host_baseline"]["auth_services"].items()
        }
        self.original_replicas = {
            service: identity["replicas"]
            for service, identity in policy["host_baseline"]["auth_services"].items()
        }
        self.images = dict(self.original_images)
        self.replicas = dict(self.original_replicas)
        self.candidate_images = {
            service: f"sha256:{index + 1:064x}"
            for index, service in enumerate(self.images)
        }
        self.marker = dict(policy["baseline"])
        self.static_mapping = "v0.5.6"
        self.traffic = "v0.5.6"
        self.beat_replicas = 1
        self.backup_retained = False
        self.backup_restore_verified = False
        self.migrations_applied = False
        self.rollback_order: list[str] = []
        self.verification: dict[str, Any] | None = None

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_at == name:
            if name == "stabilize":
                self.verification = {
                    "filename": "HEALTH.json",
                    "sha256": "6" * 64,
                    "result": "failed",
                    "iterations": 2,
                    "allowed_log_findings": 1,
                    "warnings": [
                        "expected Discord guild-owner nickname 50013 during worker-cutover"
                    ],
                    "failure": "synthetic stabilization failure",
                }
            raise contracts.DeploymentError(f"synthetic {name} failure")

    def validate(self, bundle: contracts.ValidatedBundle) -> None:
        self._call("validate")
        transition = bundle.request.recovery_transition
        assert transition is not None
        assert transition["purpose"] == "production-recovery"
        assert transition["releases"][0] == recovery_policy.load_policy()["baseline"]

    def prepare_candidate(self, _bundle: contracts.ValidatedBundle) -> None:
        self._call("prepare_candidate")

    def backup(self, _bundle: contracts.ValidatedBundle) -> dict[str, str]:
        self._call("backup")
        self.backup_retained = True
        self.backup_restore_verified = True
        return {"filename": "database.sql.gz", "sha256": "4" * 64}

    def migrate(self, _bundle: contracts.ValidatedBundle) -> None:
        self._call("migrate")
        self.migrations_applied = True

    def candidate_health(self, _bundle: contracts.ValidatedBundle) -> None:
        self._call("candidate_health")

    def switch_traffic(self, _bundle: contracts.ValidatedBundle) -> None:
        self._call("switch_traffic")
        self.traffic = "candidate"
        self.static_mapping = "candidate-versioned"

    def replace_workers(self, _bundle: contracts.ValidatedBundle) -> None:
        self._call("replace_workers")
        self.images = dict(self.candidate_images)
        self.beat_replicas = 1

    def stabilize(self, _bundle: contracts.ValidatedBundle) -> dict[str, Any]:
        self._call("stabilize")
        self.verification = {
            "filename": "HEALTH.json",
            "sha256": "5" * 64,
            "result": "success",
            "iterations": 21,
            "allowed_log_findings": 0,
            "warnings": [],
            "failure": None,
        }
        return self.verification

    def stabilization_evidence(self) -> dict[str, Any] | None:
        return self.verification

    def promote_web(self, _bundle: contracts.ValidatedBundle) -> None:
        self._call("promote_web")
        self.traffic = self.bundle.request.platform_version

    def finalize(self, _bundle: contracts.ValidatedBundle) -> None:
        self._call("finalize")
        if not self.backup_restore_verified or self.beat_replicas != 1:
            raise contracts.DeploymentError("synthetic final invariant failed")
        self.marker = {
            "manifest_sha256": self.bundle.request.manifest_sha256,
            "platform_version": self.bundle.request.platform_version,
            "release_commit": self.bundle.request.release_commit,
            "source_commit": self.bundle.manifest["source_commit"],
        }

    def cleanup_success(self, _bundle: contracts.ValidatedBundle) -> str:
        self._call("cleanup_success")
        return "synthetic cleanup passed"

    def rollback(self, _bundle: contracts.ValidatedBundle, last_state: str | None) -> str:
        self.calls.append(f"rollback:{last_state}")
        self.rollback_order.append("traffic")
        self.traffic = "v0.5.6"
        self.rollback_order.append("images-and-replicas")
        self.images = dict(self.original_images)
        self.replicas = dict(self.original_replicas)
        self.beat_replicas = 1
        self.rollback_order.append("static")
        self.static_mapping = "v0.5.6"
        self.rollback_order.append("marker")
        self.marker = dict(recovery_policy.load_policy()["baseline"])
        return "synthetic exact topology and static mapping restored"


class CoordinatedRecoveryRehearsal(unittest.TestCase):
    maxDiff = 2000

    def test_complete_recovery_and_next_release_sequence(self) -> None:
        policy = recovery_policy.load_policy()
        self.assertEqual(
            policy["server_evidence"]["sha256"],
            "df9865e121e068cc65c3e6b05cb287f1b87f05772e54ecd4c7d07fa61137fc38",
        )
        self.assertEqual(
            [item["platform_version"] for item in policy["published_releases"]],
            ["0.5.6", "0.6.0", "0.6.1"],
        )

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            checkout = base / "exact-checkout"
            cloned = run(
                "git",
                "clone",
                "--quiet",
                "--no-checkout",
                "--no-hardlinks",
                "--",
                ROOT,
                checkout,
                cwd=ROOT,
            )
            self.assertEqual(cloned.returncode, 0, cloned.stderr[-1000:])
            head = run("git", "rev-parse", "HEAD", cwd=ROOT).stdout.strip()
            self.assertEqual(
                run("git", "config", "core.autocrlf", "false", cwd=checkout).returncode,
                0,
            )
            checked_out = run(
                "git", "checkout", "--quiet", "--detach", head, cwd=checkout
            )
            self.assertEqual(checked_out.returncode, 0, checked_out.stderr[-1000:])

            manifest_061 = buh_release.verify_release_dir(
                checkout / "releases/platform/v0.6.1"
            )
            history_request = contracts.DeploymentRequest(
                mode="deploy",
                repository=policy["repository"],
                release_commit=V061_RELEASE,
                release_ref="release/platform-v0.6.1",
                platform_version="0.6.1",
                manifest_sha256=policy["published_releases"][-1]["manifest_sha256"],
                workflow_run_id="5000001",
                workflow_run_attempt=1,
            )
            history_bundle = contracts.ValidatedBundle(
                root=checkout,
                release_dir=checkout / "releases/platform/v0.6.1",
                request=history_request,
                manifest=manifest_061,
                install_plan={},
            )
            history_host = DockerHost(
                contracts.ReceiverConfig.load(
                    checkout / "ops/deploy/receiver-config.example.json"
                )
            )
            live = {
                key: policy["baseline"][key]
                for key in (
                    "manifest_sha256",
                    "platform_version",
                    "release_commit",
                    "source_commit",
                )
            }
            with mock.patch.object(history_host, "_load_current", return_value=live), self.assertRaisesRegex(
                contracts.DeploymentError, "predecessor does not match"
            ):
                history_host._validate_release_transition(history_bundle)
            self.assertEqual(live["platform_version"], "0.5.6")

            plan = buh_release.create_plan(
                repo_root=checkout,
                registry_path=checkout / "ops/release/apps.toml",
                compatibility_path=checkout / "platform/compatibility.toml",
                changes_dir=checkout / "changes",
                previous_manifest_path=(
                    checkout / "releases/platform/v0.6.1/RELEASE.json"
                ),
                source_commit=head,
                test_run="synthetic-coordinated-rehearsal",
            )
            self.assertEqual(plan["platform_version"], "0.6.2")
            self.assertEqual(plan["previous_platform_version"], "0.6.1")
            self.assertEqual(
                plan["deployment_predecessor"]["baseline"], policy["baseline"]
            )
            self.assertEqual(
                plan["deployment_predecessor"]["intervening_releases"],
                policy["published_releases"][1:],
            )

            archives = [base / "bootstrap-1.tar.gz", base / "bootstrap-2.tar.gz"]
            archive_metadata = []
            for output in archives:
                archive_metadata.append(
                    request_archive.build_archive(
                        root=checkout,
                        release_dir=checkout / "releases/platform/v0.6.1",
                        repository=policy["repository"],
                        release_commit=V061_RELEASE,
                        mode="preflight",
                        workflow_run_id="4323400000000000000",
                        workflow_run_attempt=1,
                        output=output,
                        bootstrap_recovery=True,
                    )
                )
            self.assertEqual(archives[0].read_bytes(), archives[1].read_bytes())
            self.assertEqual(archive_metadata[0], archive_metadata[1])
            extracted = base / "bootstrap-extracted"
            contracts.extract_archive(archives[0].read_bytes(), extracted)
            bootstrap_bundle = contracts.load_validated_bundle(
                extracted,
                contracts.ReceiverConfig.load(
                    checkout / "ops/deploy/receiver-config.example.json"
                ),
            )
            self.assertEqual(
                bootstrap_bundle.request.recovery_transition["purpose"],
                "receiver-upgrade-preflight",
            )
            self.assertEqual(bootstrap_bundle.request.mode, "preflight")

            system_root = base / "synthetic-host"
            populate_receiver(system_root, "old-receiver")
            original_receiver = receiver_fingerprint(system_root)
            config_input = base / "receiver-v1.json"
            legacy_input = base / "legacy-receiver"
            request_input = base / "request.tar.gz"
            config_input.write_text("{}\n", encoding="ascii")
            legacy_input.write_text("legacy\n", encoding="ascii")
            request_input.write_bytes(archives[0].read_bytes())
            backup_root = base / "receiver-backups"

            def failed_preflight() -> None:
                raise receiver_upgrade.UpgradeError("synthetic bootstrap rejection")

            with self.assertRaisesRegex(
                receiver_upgrade.UpgradeError, "Verified backup restored"
            ):
                receiver_upgrade.execute_upgrade(
                    commit=head,
                    repo_root=checkout,
                    config=config_input,
                    legacy_receiver=legacy_input,
                    request=request_input,
                    system_root=system_root,
                    backup_root=backup_root,
                    install_callback=lambda staging: populate_receiver(
                        staging, "new-receiver"
                    ),
                    preflight_callback=failed_preflight,
                )
            self.assertEqual(receiver_fingerprint(system_root), original_receiver)

            receiver_upgrade.execute_upgrade(
                commit=head,
                repo_root=checkout,
                config=config_input,
                legacy_receiver=legacy_input,
                request=request_input,
                system_root=system_root,
                backup_root=backup_root,
                install_callback=lambda staging: populate_receiver(
                    staging, "new-receiver"
                ),
                preflight_callback=lambda: None,
            )
            self.assertIn(
                b"new-receiver",
                (
                    system_root / "usr/local/sbin/buh-platform-v2-receiver"
                ).read_bytes(),
            )

            compose_overlay = yaml.safe_load(
                (checkout / "ops/deploy/bootstrap/docker-compose.buh-platform-v2.yml").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                compose_overlay["services"]["nginx"]["image"],
                policy["host_baseline"]["nginx"]["image_id"],
            )
            self.assertIn(
                "./conf/buh-platform-v2/nginx:/etc/nginx/buh-platform-v2:ro",
                compose_overlay["services"]["nginx"]["volumes"],
            )
            self.assertEqual(
                policy["host_baseline"]["compose_files_before_activation"],
                ["docker-compose.yml", "docker-compose.buh-vps-health.yml"],
            )
            self.assertEqual(
                policy["host_baseline"]["compose_files_after_activation"][-1],
                "docker-compose.buh-platform-v2.yml",
            )

            health_host = DockerHost(
                contracts.ReceiverConfig.load(
                    checkout / "ops/deploy/receiver-config.example.json"
                )
            )
            health_host.recovery_baseline_verified = True
            health_host.log_since = "2026-09-07T22:17:17+00:00"
            owner_lines = (
                "update_nickname failed for user Fifty5D, retrying in 60 secs\n"
                "Discord HTTP 403, code 50013: Missing Permissions\n"
            )

            def worker_logs(*arguments, **_kwargs):
                return (
                    owner_lines
                    if arguments[-1] == health_host.config.worker_service
                    else ""
                )

            with mock.patch.object(health_host, "_compose", side_effect=worker_logs):
                visible = health_host._scan_new_logs(
                    health_host.config.auth_services,
                    owner_transition_phase="candidate-health",
                )
                self.assertEqual(len(visible), 1)
                self.assertIn("guild-owner nickname 50013", visible[0])
                with self.assertRaisesRegex(
                    contracts.DeploymentError, "fatal AllianceAuth log"
                ):
                    health_host._scan_new_logs(health_host.config.auth_services)

            target = {
                "manifest_sha256": "7" * 64,
                "platform_version": plan["platform_version"],
                "release_commit": "8" * 40,
                "release_ref": f"release/platform-v{plan['platform_version']}",
                "source_commit": head,
            }
            transition = {
                "policy_id": policy["policy_id"],
                "policy_sha256": recovery_policy.policy_sha256(),
                "purpose": "production-recovery",
                "releases": [*policy["published_releases"], target],
            }
            deployment_request = contracts.DeploymentRequest(
                mode="deploy",
                repository=policy["repository"],
                release_commit=target["release_commit"],
                release_ref=target["release_ref"],
                platform_version=target["platform_version"],
                manifest_sha256=target["manifest_sha256"],
                workflow_run_id="6000001",
                workflow_run_attempt=1,
                recovery_transition=transition,
            )
            deployment_bundle = contracts.ValidatedBundle(
                root=base,
                release_dir=base / "release",
                request=deployment_request,
                manifest={
                    "source_commit": head,
                    "previous_release": {
                        key: policy["published_releases"][-1][key]
                        for key in (
                            "manifest_sha256",
                            "platform_version",
                            "source_commit",
                        )
                    },
                    "deployment_recovery": recovery_policy.manifest_recovery(policy),
                },
                install_plan={},
            )

            preflight_bundle = dataclasses.replace(
                deployment_bundle,
                request=dataclasses.replace(
                    deployment_request,
                    mode="preflight",
                    workflow_run_id="6000002",
                ),
            )
            preflight_backend = SyntheticFleet(preflight_bundle)
            PreflightEngine(
                preflight_backend,
                PreflightJournal(base / "preflight-state", preflight_bundle),
            ).run(preflight_bundle)
            self.assertEqual(preflight_backend.marker, policy["baseline"])
            self.assertEqual(preflight_backend.traffic, "v0.5.6")

            ready_config = approval_fixture.approval._config(
                owner=approval_fixture.OWNER,
                repository=approval_fixture.REPOSITORY,
                version=approval_fixture.VERSION,
                source_commit=approval_fixture.SOURCE,
                release_commit=approval_fixture.RELEASE,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=base / "ready.json",
            )
            ready_report = approval_fixture.approval.ready(
                ready_config,
                approval_fixture.TOKEN,
                number=approval_fixture.PR_NUMBER,
                feature_readiness=approval_fixture._published_readiness(),
                main_validation_run_id=approval_fixture.MAIN_RUN,
                main_validation_run_attempt=1,
                manifest_sha256=approval_fixture.MANIFEST,
                preflight_run_id=approval_fixture.PREFLIGHT_RUN,
                preflight_run_attempt=1,
                preflight_artifact=approval_fixture.ARTIFACT,
                transport=approval_fixture.ReadyTransport(),
                sleeper=lambda _: None,
                polls=1,
            )
            self.assertTrue(
                ready_report["approval_marker"].startswith(
                    approval_fixture.approval.APPROVAL_PREFIX
                )
            )
            with self.assertRaises(approval_fixture.sync.SyncPrError):
                approval_fixture.approval.ready(
                    ready_config,
                    approval_fixture.TOKEN,
                    number=approval_fixture.PR_NUMBER,
                    feature_readiness=approval_fixture._published_readiness(),
                    main_validation_run_id=approval_fixture.MAIN_RUN,
                    main_validation_run_attempt=1,
                    manifest_sha256=approval_fixture.MANIFEST,
                    preflight_run_id=approval_fixture.PREFLIGHT_RUN,
                    preflight_run_attempt=1,
                    preflight_artifact=approval_fixture.ARTIFACT,
                    transport=approval_fixture.DeniedReadyTransport(),
                    sleeper=lambda _: None,
                    polls=1,
                )

            authorization_index = 0

            def authorize(transport):
                nonlocal authorization_index
                authorization_index += 1
                return approval_fixture.approval.authorize(
                    approval_fixture._event(),
                    owner=approval_fixture.OWNER,
                    repository=approval_fixture.REPOSITORY,
                    actor=approval_fixture.OWNER,
                    triggering_actor=approval_fixture.OWNER,
                    run_attempt=1,
                    api_url="https://api.github.com",
                    server_url="https://github.com",
                    output=base / f"approval-{authorization_index}.json",
                    token=approval_fixture.TOKEN,
                    transport=transport,
                    sleeper=lambda _: None,
                )

            with self.assertRaisesRegex(
                approval_fixture.approval.ApprovalError,
                "lacks the ChatGPT approval marker",
            ):
                authorize(approval_fixture.ApprovalTransport(include_merge_marker=False))
            authorized = authorize(approval_fixture.ApprovalTransport())
            self.assertEqual(authorized["release_commit"], approval_fixture.RELEASE)
            changed_evidence = approval_fixture.ApprovalTransport()
            changed_evidence.artifact_digest = "sha256:" + "a" * 64
            with self.assertRaisesRegex(
                approval_fixture.approval.ApprovalError, "artifact digest"
            ):
                authorize(changed_evidence)

            failed_backend = SyntheticFleet(deployment_bundle, fail_at="stabilize")
            with self.assertRaisesRegex(
                contracts.DeploymentError, "synthetic stabilize failure"
            ):
                DeploymentEngine(
                    failed_backend,
                    DeploymentJournal(base / "failed-state", deployment_bundle),
                ).run(deployment_bundle)
            self.assertEqual(failed_backend.rollback_order[0], "traffic")
            self.assertEqual(failed_backend.images, failed_backend.original_images)
            self.assertEqual(failed_backend.replicas, failed_backend.original_replicas)
            self.assertEqual(failed_backend.static_mapping, "v0.5.6")
            self.assertEqual(failed_backend.marker, policy["baseline"])
            self.assertTrue(failed_backend.backup_retained)
            self.assertTrue(failed_backend.migrations_applied)

            successful_backend = SyntheticFleet(deployment_bundle)
            DeploymentEngine(
                successful_backend,
                DeploymentJournal(base / "success-state", deployment_bundle),
            ).run(deployment_bundle)
            self.assertEqual(successful_backend.traffic, "0.6.2")
            self.assertEqual(successful_backend.beat_replicas, 1)
            self.assertEqual(
                successful_backend.marker["release_commit"], target["release_commit"]
            )
            self.assertEqual(
                json.loads(
                    (base / "success-state/current.json").read_text(encoding="ascii")
                )["manifest_sha256"],
                target["manifest_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
